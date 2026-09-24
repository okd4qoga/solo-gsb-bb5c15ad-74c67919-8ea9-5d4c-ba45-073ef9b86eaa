"""位置交接整单冲正服务:按原交接单号把整单样品回退到原位置。

规则
----
- 一次提交冲正号、原交接单号与操作人,服务端读取原交接单**全部明细**,
  在单个 `BEGIN IMMEDIATE` 事务内逐项校验:
    1. 样品仍存在;
    2. 该样品的**最后一次位置交接仍是原单对应明细**(此后未被其他交接移动,
       原单也未被冲正过——冲正记录同样进入位置历史);
    3. 样品当前位置等于原单**目标位置**。
  任一项不符即**整单不改**,响应 `conflicts[]` 一次返回全部不符明细。
- 全部校验通过后:把全部样品恢复到原单**原位置**(含未登记的 null),
  并逐项写入**不可变**冲正审计明细(触发器禁止 UPDATE/DELETE),
  冲正记录并入按样品查询的位置历史;原交接记录保留不变。
- 冲正只改样品位置,不改动余量,不产生谱系边与出入库流水。

幂等
----
同一冲正号 + 同一请求体重放返回首次结果(`replayed: true`),不重复回退、
不重复写审计;同号不同内容返回 `409 reversal_no_conflict`;一张原交接单
只允许整单冲正一次,换冲正号重复冲正返回 `409 transfer_already_reversed`。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import schemas as s
from . import transfers as tr


class ReversalError(Exception):
    """交接冲正业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "reversal conflict")


def _conflict(
    code: str,
    message: str,
    *,
    reversal_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[str] = None,
    available: Optional[str] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=reversal_no,
        sample_id=sample_id,
        location=requested,
        requested_location=requested,
        available_location=available,
    )


# ---------------------------------------------------------------- 查询/组装

def _get_header(
    conn: sqlite3.Connection, reversal_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM location_transfer_reversals WHERE reversal_no = ?",
        (reversal_no,),
    ).fetchone()


def _load_items(
    conn: sqlite3.Connection, reversal_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM location_transfer_reversal_items WHERE reversal_no = ?"
        " ORDER BY seq_index, item_id",
        (reversal_no,),
    ).fetchall()


def _build_result(
    header: sqlite3.Row, items: list[sqlite3.Row], *, replayed: bool
) -> s.LocationTransferReversalResult:
    return s.LocationTransferReversalResult(
        reversal_no=header["reversal_no"],
        transfer_no=header["transfer_no"],
        operator=header["operator"],
        note=header["note"],
        reversed_at=header["created_at"],
        created_at=header["created_at"],
        replayed=replayed,
        items=[
            s.LocationTransferReversalItemOut(
                sample_id=r["sample_id"],
                from_location=r["from_location"],
                to_location=r["to_location"],
            )
            for r in items
        ],
    )


# ---------------------------------------------------------------- 提交冲正

def create_reversal(
    conn: sqlite3.Connection, req: s.LocationTransferReversalRequest
) -> s.LocationTransferReversalResult:
    """单事务校验原单各样品仍可回退;全部通过才统一恢复原位置并写冲正审计。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.reversal_no)
        if existing is not None:
            if existing["payload"] != payload:
                raise ReversalError([
                    _conflict(
                        "reversal_no_conflict",
                        f"冲正号 {req.reversal_no} 已存在但请求内容不同,"
                        "同一冲正号必须对应同一次交接冲正(幂等键冲突)",
                        reversal_no=req.reversal_no,
                    )
                ])
            result = _build_result(
                existing, _load_items(conn, req.reversal_no), replayed=True
            )
            conn.execute("COMMIT")
            return result

        transfer = tr._get_header(conn, req.transfer_no)
        if transfer is None:
            raise ReversalError([
                _conflict(
                    "transfer_not_found",
                    f"交接单号 {req.transfer_no} 不存在,无法冲正",
                    reversal_no=req.reversal_no,
                )
            ])

        already = conn.execute(
            "SELECT reversal_no FROM location_transfer_reversals"
            " WHERE transfer_no = ?",
            (req.transfer_no,),
        ).fetchone()
        if already is not None:
            raise ReversalError([
                _conflict(
                    "transfer_already_reversed",
                    f"交接单 {req.transfer_no} 已被冲正单"
                    f" {already['reversal_no']} 整单冲正,一张交接单只允许冲正一次",
                    reversal_no=req.reversal_no,
                )
            ])

        items = tr._load_items(conn, req.transfer_no)  # 原单全部明细(按提交顺序)

        # 先做全部静态校验(样品存在 + 最后一次交接仍是原单明细 + 当前位置等于
        # 原单目标位置),收集全部冲突后抛出
        conflicts: list[s.Conflict] = []
        for item in items:
            sid = item["sample_id"]
            sample = conn.execute(
                "SELECT location FROM samples WHERE sample_id = ?", (sid,)
            ).fetchone()
            if sample is None:  # 防御:有交接明细(FK)理论上不会走到
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {sid} 不存在,无法冲正回退",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                ))
                continue
            last = conn.execute(
                "SELECT transfer_no FROM location_transfer_items"
                " WHERE sample_id = ? ORDER BY item_id DESC LIMIT 1",
                (sid,),
            ).fetchone()
            # 原单未被冲正(上面已校验)且样品最后一次交接仍是原单明细时,
            # 不可能存在更新的冲正记录,故只需比对交接明细表
            if last is None or last["transfer_no"] != req.transfer_no:
                actual = last["transfer_no"] if last is not None else "(无)"
                conflicts.append(_conflict(
                    "last_transfer_mismatch",
                    f"样品 {sid} 的最后一次位置交接为 {actual},并非原交接单"
                    f" {req.transfer_no} 的对应明细,样品此后已被其他交接移动,"
                    "不能按原单整单冲正",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                ))
                continue
            if sample["location"] != item["to_location"]:
                cur = sample["location"]
                conflicts.append(_conflict(
                    "location_mismatch",
                    f"样品 {sid} 当前实际位置"
                    f"「{cur if cur is not None else '(未登记位置)'}」与原单目标位置"
                    f"「{item['to_location']}」不符,不能按原单整单冲正",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                    requested=item["to_location"],
                    available=cur,
                ))
        if conflicts:
            raise ReversalError(conflicts)

        # 全部通过:写不可变冲正单头与逐项审计明细,并把样品恢复到原单原位置
        conn.execute(
            "INSERT INTO location_transfer_reversals"
            " (reversal_no, transfer_no, operator, payload, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (req.reversal_no, req.transfer_no, req.operator, payload, req.note),
        )
        for item in items:
            conn.execute(
                "UPDATE samples SET location = ? WHERE sample_id = ?",
                (item["from_location"], item["sample_id"]),
            )
            conn.execute(
                "INSERT INTO location_transfer_reversal_items"
                " (reversal_no, transfer_no, sample_id, seq_index,"
                " from_location, to_location, operator)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (req.reversal_no, req.transfer_no, item["sample_id"],
                 item["seq_index"], item["to_location"], item["from_location"],
                 req.operator),
            )

        header = _get_header(conn, req.reversal_no)
        result = _build_result(
            header, _load_items(conn, req.reversal_no), replayed=False  # type: ignore[arg-type]
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 冲正查询

def get_reversal(
    conn: sqlite3.Connection, reversal_no: str
) -> s.LocationTransferReversalResult:
    """按冲正号查询首次冲正结果(幂等回放内容);冲正号不存在返回 404 冲突。"""
    header = _get_header(conn, reversal_no)
    if header is None:
        raise ReversalError([
            _conflict(
                "reversal_not_found",
                f"冲正号 {reversal_no} 不存在",
                reversal_no=reversal_no,
            )
        ])
    return _build_result(header, _load_items(conn, reversal_no), replayed=False)
