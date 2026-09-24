"""位置交接整单冲正服务:按原交接单号、冲正号和操作人提交整单回退。

规则
----
- 客户端只提交原交接单号 `transfer_no`、冲正号 `reversal_no` 与操作人,
  原单全部明细由服务端在单个 `BEGIN IMMEDIATE` 事务内读取。
- 逐项确认:
    1. 样品的**最后一次位置交接**(`location_transfer_items` 中该样品
       item_id 最大的一条)仍是原单对应明细(没有更新的交接发生过);
    2. 样品**当前位置**等于原单目标位置;
  任一项不符即**整单不改**,响应 `conflicts[]` 一次返回全部不符明细。
- 全部通过后把全部样品恢复到原单**原位置**(`from_location`,含未登记的
  null),另存不可变冲正审计(头表 + 逐项明细,触发器禁止 UPDATE/DELETE),
  冲正明细接入位置历史;**原交接记录原样保留**。
- 冲正只改位置,不改动余量,不产生谱系边与出入库流水。

幂等
----
同一冲正号 + 同一请求体重放返回首次结果(`replayed: true`),
不重复回退、不重复写审计;同号不同内容返回 `409 reversal_no_conflict`;
原单已被冲正再提交返回 `409 transfer_already_reversed`。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import schemas as s


class ReversalError(Exception):
    """位置交接冲正业务冲突,携带结构化冲突列表。"""

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
        handover_at=header["created_at"],
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
    """单事务校验原单可整单冲正;全部通过才恢复原位置并写不可变冲正审计。"""
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
                        "同一冲正号必须对应同一次位置交接冲正(幂等键冲突)",
                        reversal_no=req.reversal_no,
                    )
                ])
            result = _build_result(
                existing, _load_items(conn, req.reversal_no), replayed=True
            )
            conn.execute("COMMIT")
            return result

        original = conn.execute(
            "SELECT * FROM location_transfers WHERE transfer_no = ?",
            (req.transfer_no,),
        ).fetchone()
        if original is None:
            raise ReversalError([
                _conflict(
                    "transfer_not_found",
                    f"原交接单号 {req.transfer_no} 不存在,无法冲正",
                    reversal_no=req.reversal_no,
                )
            ])

        # 原单重复冲正:冲正审计不可变,只要该原单已有任意冲正明细即拒绝
        reversed_row = conn.execute(
            "SELECT reversal_no FROM location_transfer_reversal_items"
            " WHERE transfer_no = ? LIMIT 1",
            (req.transfer_no,),
        ).fetchone()
        if reversed_row is not None:
            raise ReversalError([
                _conflict(
                    "transfer_already_reversed",
                    f"交接单 {req.transfer_no} 已被冲正单"
                    f" {reversed_row['reversal_no']} 整单冲正,"
                    "原交接单只能冲正一次",
                    reversal_no=req.reversal_no,
                )
            ])

        orig_items = conn.execute(
            "SELECT * FROM location_transfer_items WHERE transfer_no = ?"
            " ORDER BY seq_index, item_id",
            (req.transfer_no,),
        ).fetchall()

        sample_ids = [r["sample_id"] for r in orig_items]
        qmarks = ",".join("?" * len(sample_ids))
        current_rows = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT sample_id, location FROM samples"
                f" WHERE sample_id IN ({qmarks})",
                sample_ids,
            ).fetchall()
        }
        # 每个样品的最后一次位置交接(item_id 最大者),只看交接明细本身,
        # 冲正事件另表审计,不计入"最后一次位置交接"
        last_rows = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT i.sample_id AS sample_id, i.transfer_no AS transfer_no,"
                f" i.item_id AS item_id FROM location_transfer_items i"
                f" JOIN (SELECT sample_id, MAX(item_id) AS max_id"
                f" FROM location_transfer_items WHERE sample_id IN ({qmarks})"
                f" GROUP BY sample_id) m ON m.max_id = i.item_id",
                sample_ids,
            ).fetchall()
        }

        # 逐项静态校验,收集全部冲突后一次抛出;任一不符整单不改
        conflicts: list[s.Conflict] = []
        for item in orig_items:
            sid = item["sample_id"]
            cur = current_rows.get(sid)
            if cur is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {sid} 不存在,无法随交接单 {req.transfer_no} 冲正",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                ))
                continue
            last = last_rows.get(sid)
            if last is None or last["transfer_no"] != req.transfer_no:
                last_no = last["transfer_no"] if last is not None else None
                conflicts.append(_conflict(
                    "transfer_not_last_handover",
                    f"样品 {sid} 的最后一次位置交接不是原单 {req.transfer_no}"
                    f"(最后交接单为 {last_no}),原单之后又发生过位置交接,"
                    "整单拒绝冲正",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                ))
                continue
            if cur["location"] != item["to_location"]:
                actual = cur["location"]
                conflicts.append(_conflict(
                    "location_mismatch",
                    f"样品 {sid} 当前实际位置"
                    f"「{actual if actual is not None else '(未登记位置)'}」"
                    "不等于原单目标位置"
                    f"「{item['to_location']}」,整单拒绝冲正",
                    reversal_no=req.reversal_no,
                    sample_id=sid,
                    requested=item["to_location"],
                    available=actual,
                ))
        if conflicts:
            raise ReversalError(conflicts)

        # 全部通过:恢复原位置(含 null),写冲正单头与逐项不可变审计;
        # 原交接单/原交接明细原样保留
        conn.execute(
            "INSERT INTO location_transfer_reversals"
            " (reversal_no, transfer_no, operator, payload, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (req.reversal_no, req.transfer_no, req.operator, payload, req.note),
        )
        for item in orig_items:
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
                 item["seq_index"], item["to_location"],
                 item["from_location"], req.operator),
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


# ---------------------------------------------------------------- 按冲正号查询

def get_reversal(
    conn: sqlite3.Connection, reversal_no: str
) -> s.LocationTransferReversalResult:
    """按冲正号查询首次冲正结果;不存在抛 ReversalError(reversal_not_found)。"""
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
