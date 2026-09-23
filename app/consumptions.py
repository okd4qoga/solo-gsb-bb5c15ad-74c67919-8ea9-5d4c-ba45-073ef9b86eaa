"""核销领用服务:对现有预留单做分次领用(核销)。

规则
----
- 一次提交一个核销号、一个预留号与若干样品领用量,在单个
  `BEGIN IMMEDIATE` 事务内:
    1. 校验预留单存在且**未释放**;
    2. 逐项校验样品属于该预留单,且领用量不超过该明细的**未核销量**
       (预留锁定量 − 该明细已核销合计);
    3. 逐项校验样品当前余量足够;
  任一项失败即**整单回滚**,余量、版本、核销明细与锁定量一律不变,
  响应 `conflicts[]` 一次返回全部冲突明细。
- 全部校验通过后:扣减各样品余量并递增版本,逐项写入**不可变**核销明细
  (`consumption_items`,触发器禁止 UPDATE/DELETE);该预留明细后续只把
  **未核销部分**继续计入锁定量,释放预留时仅解除剩余(未核销)锁定。
- 核销量纳入余量账本重算(非谱系出账),核销后
  `/api/v1/checks/balances` 仍保持一致。

幂等
----
同一核销号 + 同一请求体重放返回首次结果(`replayed: true`),
不重复扣减、不重复写明细;同号不同内容返回 `409 consumption_no_conflict`。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import schemas as s
from . import reservations as rsv

_EPS = 1e-9  # 运算时浮点比较


class ConsumptionError(Exception):
    """核销领用业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "consumption conflict")


def _r(x: float) -> float:
    """统一保留 9 位小数,消除浮点尾巴。"""
    return round(float(x), 9)


def _conflict(
    code: str,
    message: str,
    *,
    consumption_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=consumption_no,
        sample_id=sample_id,
        requested=requested,
        available=available,
    )


# ---------------------------------------------------------------- 查询/组装

def _get_header(
    conn: sqlite3.Connection, consumption_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM consumptions WHERE consumption_no = ?", (consumption_no,)
    ).fetchone()


def _result_from_snapshot(
    header: sqlite3.Row, *, replayed: bool
) -> s.ConsumptionResult:
    """从头表保存的首次结果快照还原响应(重放直接返回原结果)。"""
    snap = json.loads(header["result_snapshot"])
    return s.ConsumptionResult(
        consumption_no=header["consumption_no"],
        reservation_no=header["reservation_no"],
        status=snap["status"],
        items=[s.ConsumptionItemOut(**i) for i in snap["items"]],
        operator=header["operator"],
        note=header["note"],
        replayed=replayed,
        created_at=header["created_at"],
    )


# ---------------------------------------------------------------- 提交核销

def create_consumption(
    conn: sqlite3.Connection, req: s.ConsumptionRequest
) -> s.ConsumptionResult:
    """单事务校验预留未释放/逐项未核销量/样品余量,任一失败整单不变。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.consumption_no)
        if existing is not None:
            if existing["payload"] != payload:
                raise ConsumptionError([
                    _conflict(
                        "consumption_no_conflict",
                        f"核销号 {req.consumption_no} 已存在但请求内容不同,"
                        "同一核销号必须对应同一次核销领用(幂等键冲突)",
                        consumption_no=req.consumption_no,
                    )
                ])
            result = _result_from_snapshot(existing, replayed=True)
            conn.execute("COMMIT")
            return result

        reservation = conn.execute(
            "SELECT * FROM reservations WHERE reservation_no = ?",
            (req.reservation_no,),
        ).fetchone()
        if reservation is None:
            raise ConsumptionError([
                _conflict(
                    "reservation_not_found",
                    f"预留号 {req.reservation_no} 不存在,无法核销领用",
                    consumption_no=req.consumption_no,
                )
            ])
        if reservation["status"] != "active":
            raise ConsumptionError([
                _conflict(
                    "reservation_closed",
                    f"预留号 {req.reservation_no} 已释放,不能再核销领用",
                    consumption_no=req.consumption_no,
                )
            ])

        # 预留明细(样品 -> 原始锁定量/单位),已核销合计
        ritems = {
            r["sample_id"]: r
            for r in conn.execute(
                "SELECT * FROM reservation_items WHERE reservation_no = ?",
                (req.reservation_no,),
            ).fetchall()
        }
        consumed_so_far = rsv._item_consumed_map(conn, req.reservation_no)

        ids = list(dict.fromkeys(i.sample_id for i in req.items))  # model 已保证不重复
        qmarks = ",".join("?" * len(ids))
        samples = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT * FROM samples WHERE sample_id IN ({qmarks})", ids
            ).fetchall()
        }

        # 先做全部静态校验(属于预留单 + 未核销量 + 样品余量),收集全部冲突后抛出
        conflicts: list[s.Conflict] = []
        for item in req.items:
            amount = _r(item.amount)
            ritem = ritems.get(item.sample_id)
            if ritem is None:
                # 样品可能完全不存在,或存在但不在本预留单中
                if item.sample_id not in samples:
                    conflicts.append(_conflict(
                        "sample_not_found",
                        f"样品 {item.sample_id} 不存在,无法核销领用",
                        consumption_no=req.consumption_no,
                        sample_id=item.sample_id, requested=amount,
                    ))
                else:
                    conflicts.append(_conflict(
                        "reservation_item_not_found",
                        f"样品 {item.sample_id} 不在预留单 {req.reservation_no} 中,"
                        "核销明细必须逐项属于该预留单",
                        consumption_no=req.consumption_no,
                        sample_id=item.sample_id, requested=amount,
                    ))
                continue

            already = consumed_so_far.get(item.sample_id, 0.0)
            unconsumed = _r(_r(ritem["amount"]) - already)
            if amount > unconsumed + _EPS:
                conflicts.append(_conflict(
                    "insufficient_unconsumed",
                    f"样品 {item.sample_id} 在预留单 {req.reservation_no} 中"
                    f"未核销量仅剩 {unconsumed} {ritem['unit']},"
                    f"本次核销领用需要 {amount} {ritem['unit']}"
                    f"(原锁定 {_r(ritem['amount'])},已核销 {already})",
                    consumption_no=req.consumption_no,
                    sample_id=item.sample_id,
                    requested=amount, available=unconsumed,
                ))

            sample = samples.get(item.sample_id)
            if sample is None:  # 防御:有预留明细(FK)理论上不会走到
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {item.sample_id} 不存在,无法核销领用",
                    consumption_no=req.consumption_no,
                    sample_id=item.sample_id, requested=amount,
                ))
                continue
            if amount > _r(sample["remaining"]) + _EPS:
                conflicts.append(_conflict(
                    "insufficient_balance",
                    f"样品 {item.sample_id} 当前余量 {_r(sample['remaining'])}"
                    f" {sample['unit']} 不足,本次核销领用需要 {amount}"
                    f" {sample['unit']}",
                    consumption_no=req.consumption_no,
                    sample_id=item.sample_id,
                    requested=amount, available=_r(sample["remaining"]),
                ))
        if conflicts:
            raise ConsumptionError(conflicts)

        # 本地累计各明细已核销量与样品余量,供单内后续明细及最终结果使用
        consumed_view = dict(consumed_so_far)
        remaining_view = {sid: _r(r["remaining"]) for sid, r in samples.items()}
        version_view = {sid: r["version"] for sid, r in samples.items()}

        item_rows: list[dict] = []
        for idx, item in enumerate(req.items):
            sample = samples[item.sample_id]
            ritem = ritems[item.sample_id]
            amount = _r(item.amount)
            from_version = version_view[item.sample_id]
            to_version = from_version + 1
            remaining_after = _r(remaining_view[item.sample_id] - amount)
            consumed_before = consumed_view.get(item.sample_id, 0.0)
            consumed_total = _r(consumed_before + amount)
            consumed_view[item.sample_id] = consumed_total
            remaining_view[item.sample_id] = remaining_after
            version_view[item.sample_id] = to_version
            item_rows.append({
                "seq_index": idx,
                "sample_id": item.sample_id,
                "amount": amount,
                "unit": sample["unit"],
                "locked_amount": _r(ritem["amount"]),
                "consumed_before": consumed_before,
                "consumed_total": consumed_total,
                "remaining_locked": _r(_r(ritem["amount"]) - consumed_total),
                "remaining_after": remaining_after,
                "from_version": from_version,
                "to_version": to_version,
            })

        # 核销后各样品未释放预留的未核销锁定总量:库视图尚在核销前,
        # 本单对每样品恰好新增 amount 的已核销(请求内样品不重复),
        # 故锁定后量 = 核销前锁定 − amount;余量同步扣减,可用量不变
        avail_before = rsv._availability_map(conn, ids)
        result_items: list[s.ConsumptionItemOut] = []
        for row in item_rows:
            reserved_after = _r(
                avail_before[row["sample_id"]]["reserved"] - row["amount"]
            )
            result_items.append(s.ConsumptionItemOut(
                sample_id=row["sample_id"],
                amount=row["amount"],
                unit=row["unit"],
                locked_amount=row["locked_amount"],
                consumed_before=row["consumed_before"],
                consumed_total=row["consumed_total"],
                remaining_locked=row["remaining_locked"],
                remaining=row["remaining_after"],
                reserved=reserved_after,
                available=_r(row["remaining_after"] - reserved_after),
                from_version=row["from_version"],
                to_version=row["to_version"],
            ))

        created_at = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS ts"
        ).fetchone()["ts"]
        result = s.ConsumptionResult(
            consumption_no=req.consumption_no,
            reservation_no=req.reservation_no,
            status="active",
            items=result_items,
            operator=req.operator,
            note=req.note,
            replayed=False,
            created_at=created_at,
        )
        snapshot = json.dumps(
            result.model_dump(exclude={"replayed"}),
            ensure_ascii=False,
        )

        # 全部通过:写不可变核销单头与明细,扣减余量并递增版本
        conn.execute(
            "INSERT INTO consumptions (consumption_no, reservation_no, payload,"
            " result_snapshot, operator, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (req.consumption_no, req.reservation_no, payload, snapshot,
             req.operator, req.note, created_at),
        )
        for row in item_rows:
            conn.execute(
                "INSERT INTO consumption_items (consumption_no, reservation_no,"
                " sample_id, amount, unit, seq_index, from_version, to_version,"
                " remaining_after, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (req.consumption_no, req.reservation_no, row["sample_id"],
                 row["amount"], row["unit"], row["seq_index"],
                 row["from_version"], row["to_version"],
                 row["remaining_after"], created_at),
            )
            conn.execute(
                "UPDATE samples SET remaining = remaining - ?,"
                " version = version + 1 WHERE sample_id = ?",
                (row["amount"], row["sample_id"]),
            )

        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询

def get_consumption(
    conn: sqlite3.Connection, consumption_no: str
) -> s.ConsumptionResult:
    header = _get_header(conn, consumption_no)
    if header is None:
        raise ConsumptionError([
            _conflict(
                "consumption_not_found",
                f"核销号 {consumption_no} 不存在",
                consumption_no=consumption_no,
            )
        ])
    return _result_from_snapshot(header, replayed=False)
