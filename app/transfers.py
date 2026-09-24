"""位置交接服务:一次提交多个样品的原位置与目标位置。

规则
----
- 单个 `BEGIN IMMEDIATE` 事务内逐项校验样品**仍处于请求所述原位置**
  (样品存在且 `samples.location` 与请求的 from_location 完全一致);
  任一项不符(样品不存在或已被其他交接/操作移动)即**整批回滚**,
  所有样品位置保持不变,响应 `conflicts[]` 一次返回全部不符明细。
- 全部校验通过后才统一更新各样品位置,并逐项写入不可变交接审计明细
  (交接人、交接时间、变更前位置、变更后位置)。
- 交接只改样品位置,不改动余量,不产生谱系边与出入库流水。

幂等
----
同一交接单号 + 同一请求体重放返回首次结果(`replayed: true`),
不重复移动、不重复写审计;同号不同内容返回 `409 transfer_no_conflict`。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import schemas as s


class TransferError(Exception):
    """位置交接业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "transfer conflict")


def _conflict(
    code: str,
    message: str,
    *,
    transfer_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[str] = None,
    available: Optional[str] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=transfer_no,
        sample_id=sample_id,
        location=requested,
        requested_location=requested,
        available_location=available,
    )


# ---------------------------------------------------------------- 查询/组装

def _get_header(
    conn: sqlite3.Connection, transfer_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM location_transfers WHERE transfer_no = ?", (transfer_no,)
    ).fetchone()


def _load_items(
    conn: sqlite3.Connection, transfer_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM location_transfer_items WHERE transfer_no = ?"
        " ORDER BY seq_index, item_id",
        (transfer_no,),
    ).fetchall()


def _build_result(
    header: sqlite3.Row, items: list[sqlite3.Row], *, replayed: bool
) -> s.LocationTransferResult:
    return s.LocationTransferResult(
        transfer_no=header["transfer_no"],
        operator=header["operator"],
        note=header["note"],
        handover_at=header["created_at"],
        created_at=header["created_at"],
        replayed=replayed,
        items=[
            s.LocationTransferItemOut(
                sample_id=r["sample_id"],
                from_location=r["from_location"],
                to_location=r["to_location"],
            )
            for r in items
        ],
    )


# ---------------------------------------------------------------- 提交交接

def create_transfer(
    conn: sqlite3.Connection, req: s.LocationTransferRequest
) -> s.LocationTransferResult:
    """单事务校验全部样品仍在请求所述原位置;全部通过才统一更新并写审计。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.transfer_no)
        if existing is not None:
            if existing["payload"] != payload:
                raise TransferError([
                    _conflict(
                        "transfer_no_conflict",
                        f"交接单号 {req.transfer_no} 已存在但请求内容不同,"
                        "同一交接单号必须对应同一次位置交接(幂等键冲突)",
                        transfer_no=req.transfer_no,
                    )
                ])
            result = _build_result(
                existing, _load_items(conn, req.transfer_no), replayed=True
            )
            conn.execute("COMMIT")
            return result

        ids = list(dict.fromkeys(i.sample_id for i in req.items))  # model 已保证不重复
        qmarks = ",".join("?" * len(ids))
        rows = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT sample_id, location FROM samples WHERE sample_id IN ({qmarks})",
                ids,
            ).fetchall()
        }

        # 先做全部静态校验(样品存在 + 仍处于请求所述原位置),收集全部冲突后抛出
        conflicts: list[s.Conflict] = []
        for item in req.items:
            row = rows.get(item.sample_id)
            if row is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {item.sample_id} 不存在,无法交接位置",
                    transfer_no=req.transfer_no,
                    sample_id=item.sample_id,
                    requested=item.from_location,
                ))
                continue
            if row["location"] != item.from_location:
                cur = row["location"]
                conflicts.append(_conflict(
                    "location_mismatch",
                    f"样品 {item.sample_id} 当前实际位置"
                    f"「{cur if cur is not None else '(未登记位置)'}」与请求所述原位置"
                    f"「{item.from_location if item.from_location is not None else '(未登记位置)'}」"
                    "不符,整批拒绝交接",
                    transfer_no=req.transfer_no,
                    sample_id=item.sample_id,
                    requested=item.from_location,
                    available=cur,
                ))
        if conflicts:
            raise TransferError(conflicts)

        # 全部通过:统一更新位置,并写交接单头与逐项不可变审计明细
        conn.execute(
            "INSERT INTO location_transfers (transfer_no, operator, payload, note)"
            " VALUES (?, ?, ?, ?)",
            (req.transfer_no, req.operator, payload, req.note),
        )
        for idx, item in enumerate(req.items):
            conn.execute(
                "UPDATE samples SET location = ? WHERE sample_id = ?",
                (item.to_location, item.sample_id),
            )
            conn.execute(
                "INSERT INTO location_transfer_items (transfer_no, sample_id,"
                " seq_index, from_location, to_location, operator)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (req.transfer_no, item.sample_id, idx,
                 item.from_location, item.to_location, req.operator),
            )

        header = _get_header(conn, req.transfer_no)
        result = _build_result(
            header, _load_items(conn, req.transfer_no), replayed=False  # type: ignore[arg-type]
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 交接历史

def get_history(
    conn: sqlite3.Connection, sample_id: str
) -> s.LocationHistoryResponse:
    """按样品查询位置交接/冲正历史(最新在前);样品不存在返回 404 冲突。"""
    sample = conn.execute(
        "SELECT location FROM samples WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise TransferError([
            _conflict(
                "sample_not_found",
                f"样品 {sample_id} 不存在",
                sample_id=sample_id,
            )
        ])

    # 交接明细与冲正明细合并为统一位置历史;同毫秒时冲正记录排在交接之前
    # (冲正必晚于被冲正的交接),同类再按表内自增 id 倒序
    rows = conn.execute(
        "SELECT kind, transfer_no, reversal_no, from_location, to_location,"
        " operator, note, created_at FROM ("
        " SELECT 'transfer' AS kind, i.transfer_no AS transfer_no,"
        " NULL AS reversal_no, i.from_location AS from_location,"
        " i.to_location AS to_location, i.operator AS operator,"
        " t.note AS note, i.created_at AS created_at, i.item_id AS item_id"
        " FROM location_transfer_items i"
        " JOIN location_transfers t ON t.transfer_no = i.transfer_no"
        " WHERE i.sample_id = ?"
        " UNION ALL"
        " SELECT 'reversal' AS kind, r.transfer_no AS transfer_no,"
        " i.reversal_no AS reversal_no, i.from_location AS from_location,"
        " i.to_location AS to_location, i.operator AS operator,"
        " r.note AS note, i.created_at AS created_at, i.item_id AS item_id"
        " FROM location_transfer_reversal_items i"
        " JOIN location_transfer_reversals r ON r.reversal_no = i.reversal_no"
        " WHERE i.sample_id = ?"
        " ) ORDER BY created_at DESC,"
        " CASE kind WHEN 'reversal' THEN 0 ELSE 1 END, item_id DESC",
        (sample_id, sample_id),
    ).fetchall()
    return s.LocationHistoryResponse(
        sample_id=sample_id,
        current_location=sample["location"],
        history=[
            s.LocationHistoryItem(
                kind=r["kind"],
                transfer_no=r["transfer_no"],
                reversal_no=r["reversal_no"],
                operator=r["operator"],
                note=r["note"],
                from_location=r["from_location"],
                to_location=r["to_location"],
                handover_at=r["created_at"],
            )
            for r in rows
        ],
    )
