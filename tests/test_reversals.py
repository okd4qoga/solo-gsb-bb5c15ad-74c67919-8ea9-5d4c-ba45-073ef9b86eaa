"""位置交接整单冲正端到端测试:

- 按原交接单号/冲正号/操作人提交,服务端读取原单全部明细,
  通过后把全部样品恢复到原单原位置(含未登记 null),不动余量
- 单事务确认每个样品的最后一次位置交接仍是原单对应明细,
  且当前位置等于原单目标位置;任一不符逐项冲突、整单不改
- 不可变冲正审计(头表/明细表触发器禁止 UPDATE/DELETE),
  冲正接入位置历史,原交接记录保留
- 同冲正号同内容重放不重复回退;同号异内容 409;原单重复冲正 409
- 按冲正号查询接口;原交接单不存在 404、冲正号不存在 404
- 旧版本数据库启动后自动建表(冲正表随初始化幂等创建)
"""
from __future__ import annotations

import pytest
import sqlite3
from fastapi.testclient import TestClient

from app import transfers as tr
from app import reversals as rev
from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sample(client, op_no, sid, amount=100, location=None, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit, "location": location,
    })
    assert r.status_code == 200, r.text


def _transfer(client, no, operator, items, **extra):
    r = client.post("/api/v1/transfers", json={
        "transfer_no": no, "operator": operator, "items": items, **extra,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _reverse(client, reversal_no, transfer_no, operator="冲正人", **extra):
    return client.post("/api/v1/transfers/reversals", json={
        "reversal_no": reversal_no, "transfer_no": transfer_no,
        "operator": operator, **extra,
    })


def _locations(client):
    return {
        x["sample_id"]: x["location"]
        for x in client.get("/api/v1/samples").json()["items"]
    }


# ---------------------------------------------------------------- 成功路径

def test_reversal_restores_all_samples_to_original_locations(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", 50, location="冷库A-2")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
    ], note="冷库迁移")

    r = _reverse(client, "RV-1", "TR-1", note="交接错误,整单撤回")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversal_no"] == "RV-1"
    assert body["transfer_no"] == "TR-1"
    assert body["operator"] == "冲正人"
    assert body["note"] == "交接错误,整单撤回"
    assert body["replayed"] is False
    assert body["handover_at"] == body["created_at"]
    assert body["items"] == [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库A-1"},
        {"sample_id": "S2", "from_location": "冷库B-2", "to_location": "冷库A-2"},
    ]

    # 全部样品恢复到原单原位置
    assert _locations(client) == {"S1": "冷库A-1", "S2": "冷库A-2"}


def test_reversal_restores_unregistered_null_location(client):
    # 原单从 null(未登记)交接出去,冲正后恢复为 null
    _sample(client, "OP-1", "S1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": None, "to_location": "冷库B-1"},
    ])
    assert _locations(client)["S1"] == "冷库B-1"

    r = _reverse(client, "RV-1", "TR-1")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0] == {
        "sample_id": "S1", "from_location": "冷库B-1", "to_location": None,
    }
    assert _locations(client)["S1"] is None


def test_reversal_does_not_change_remaining_and_ledger_stays_consistent(client):
    _sample(client, "OP-1", "S1", 100, location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    assert _reverse(client, "RV-1", "TR-1").status_code == 200

    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True
    assert next(b for b in bal["samples"] if b["sample_id"] == "S1")["stored_remaining"] == 100.0


def test_reversal_keeps_original_transfer_records(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    _reverse(client, "RV-1", "TR-1")

    conn = next(app.dependency_overrides[get_conn]())
    # 原交接单头与明细原样保留
    assert conn.execute(
        "SELECT COUNT(*) c FROM location_transfers WHERE transfer_no = 'TR-1'"
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM location_transfer_items WHERE transfer_no = 'TR-1'"
    ).fetchone()["c"] == 1
    # 冲正头与明细各一份
    assert conn.execute(
        "SELECT transfer_no, operator FROM location_transfer_reversals"
        " WHERE reversal_no = 'RV-1'"
    ).fetchone()["transfer_no"] == "TR-1"
    row = conn.execute(
        "SELECT * FROM location_transfer_reversal_items WHERE reversal_no = 'RV-1'"
    ).fetchone()
    assert row["from_location"] == "冷库B-1"
    assert row["to_location"] == "冷库A-1"


# ---------------------------------------------------------------- 逐项冲突 / 整单不改

def test_reversal_conflict_when_later_handover_exists(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", location="冷库A-2")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
    ])
    # S1 又发生一次交接;S2 仍在原单目标位置
    _transfer(client, "TR-2", "李接收", [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库C-1"},
    ])

    r = _reverse(client, "RV-1", "TR-1")
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "transfer_not_last_handover"
    assert c["sample_id"] == "S1"
    assert c["op_no"] == "RV-1"
    assert "TR-2" in c["message"]

    # 整单不改:S2 也没有被回退;不写任何冲正记录
    assert _locations(client) == {"S1": "冷库C-1", "S2": "冷库B-2"}
    conn = next(app.dependency_overrides[get_conn]())
    assert conn.execute(
        "SELECT COUNT(*) c FROM location_transfer_reversals"
    ).fetchone()["c"] == 0


def test_reversal_conflict_when_current_location_differs_from_target(client):
    # 当前位置 != 原单目标位置但最后交接恰好又是原单:
    # 先 TR-1 移走,再冲正(回 A),再 TR-2 把 S1 从 A 移到别处后,
    # 用一笔与 TR-1 同路径的新单 TR-3 使「最后交接单≠TR-1」覆盖不到;
    # 这里直接构造:TR-1(A->B),随后无新交接,但样品被直接改库模拟外部移位
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    conn = next(app.dependency_overrides[get_conn]())
    conn.execute("UPDATE samples SET location = '冷库Z-9' WHERE sample_id = 'S1'")

    r = _reverse(client, "RV-1", "TR-1")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "location_mismatch"
    assert c["sample_id"] == "S1"
    assert c["requested_location"] == "冷库B-1"
    assert c["available_location"] == "冷库Z-9"
    assert "冷库Z-9" in c["message"] and "冷库B-1" in c["message"]
    assert _locations(client)["S1"] == "冷库Z-9"


def test_reversal_collects_all_item_conflicts(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", location="冷库A-2")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
    ])
    _transfer(client, "TR-2", "李接收", [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库C-1"},
    ])
    conn = next(app.dependency_overrides[get_conn]())
    conn.execute("UPDATE samples SET location = '冷库Z-9' WHERE sample_id = 'S2'")

    r = _reverse(client, "RV-1", "TR-1")
    assert r.status_code == 409
    by_sample = {c["sample_id"]: c["code"] for c in r.json()["conflicts"]}
    assert by_sample == {
        "S1": "transfer_not_last_handover",
        "S2": "location_mismatch",
    }
    # 整单不改
    assert _locations(client) == {"S1": "冷库C-1", "S2": "冷库Z-9"}


def test_reversal_unknown_transfer_is_404(client):
    r = _reverse(client, "RV-1", "NOPE")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "transfer_not_found"
    assert body["conflicts"][0]["code"] == "transfer_not_found"


# ---------------------------------------------------------------- 幂等

def test_reversal_idempotent_replay_does_not_roll_back_twice(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    body = {"reversal_no": "RV-ID", "transfer_no": "TR-1", "operator": "冲正人"}
    r1 = client.post("/api/v1/transfers/reversals", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False
    first_time = r1.json()["handover_at"]
    assert _locations(client)["S1"] == "冷库A-1"

    # 冲正后样品已回到 A 且又发生了新的交接;同冲正号重放只回放原结果,不再移动
    _transfer(client, "TR-2", "李接收", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库D-1"},
    ])
    r2 = client.post("/api/v1/transfers/reversals", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["handover_at"] == first_time
    assert _locations(client)["S1"] == "冷库D-1"

    # 只写过一次冲正明细
    conn = next(app.dependency_overrides[get_conn]())
    assert conn.execute(
        "SELECT COUNT(*) c FROM location_transfer_reversal_items WHERE reversal_no = 'RV-ID'"
    ).fetchone()["c"] == 1


def test_reversal_same_no_different_payload_conflicts(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    assert _reverse(client, "RV-C", "TR-1", operator="冲正人").status_code == 200
    # 同冲正号、不同操作人(内容不同)
    r = _reverse(client, "RV-C", "TR-1", operator="另一个人")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "reversal_no_conflict"


def test_reversal_original_transfer_can_only_be_reversed_once(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    assert _reverse(client, "RV-1", "TR-1").status_code == 200

    # 原单重复冲正(新冲正号)
    r = _reverse(client, "RV-2", "TR-1")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "transfer_already_reversed"
    assert "RV-1" in c["message"]


def test_reversal_then_new_transfer_then_reversal_of_new_transfer(client):
    # TR-1 冲正后,样品回到 A;新交接 TR-3 可以再被独立冲正
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    assert _reverse(client, "RV-1", "TR-1").status_code == 200
    _transfer(client, "TR-3", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库E-1"},
    ])
    r = _reverse(client, "RV-3", "TR-3")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == [
        {"sample_id": "S1", "from_location": "冷库E-1", "to_location": "冷库A-1"},
    ]
    assert _locations(client)["S1"] == "冷库A-1"


# ---------------------------------------------------------------- 历史接入

def test_history_includes_reversal_with_kind_newest_first(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ], note="迁移")
    _reverse(client, "RV-1", "TR-1", operator="冲正人", note="撤回")

    body = client.get("/api/v1/samples/S1/transfers").json()
    assert body["current_location"] == "冷库A-1"
    assert len(body["history"]) == 2
    newest, oldest = body["history"]
    assert newest["kind"] == "reversal"
    assert newest["transfer_no"] == "TR-1"
    assert newest["reversal_no"] == "RV-1"
    assert newest["operator"] == "冲正人"
    assert newest["note"] == "撤回"
    assert newest["from_location"] == "冷库B-1"
    assert newest["to_location"] == "冷库A-1"
    assert oldest["kind"] == "transfer"
    assert oldest["reversal_no"] is None
    assert oldest["transfer_no"] == "TR-1"


# ---------------------------------------------------------------- 按冲正号查询

def test_get_reversal_by_no(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    _reverse(client, "RV-1", "TR-1", operator="冲正人", note="撤回")

    r = client.get("/api/v1/transfers/reversals/RV-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversal_no"] == "RV-1"
    assert body["transfer_no"] == "TR-1"
    assert body["replayed"] is False
    assert body["items"] == [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库A-1"},
    ]


def test_get_unknown_reversal_is_404(client):
    r = client.get("/api/v1/transfers/reversals/GHOST")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "reversal_not_found"
    assert body["conflicts"][0]["code"] == "reversal_not_found"


# ---------------------------------------------------------------- 不可变审计

def test_reversal_audit_tables_are_immutable(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, "TR-1", "王交接", [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    assert _reverse(client, "RV-1", "TR-1").status_code == 200

    conn = next(app.dependency_overrides[get_conn]())
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE location_transfer_reversal_items SET to_location = 'z'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfer_reversal_items")
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE location_transfer_reversals SET operator = 'z'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfer_reversals")


# ---------------------------------------------------------------- 旧库迁移

def test_init_db_creates_reversal_tables_on_legacy_database(tmp_path):
    """旧版本数据库(仅有样品/交接表)启动初始化后,冲正表自动补齐且全流程可用。"""
    db_path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " location TEXT, is_root INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
    )
    raw.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, location, is_root) VALUES"
        " ('S1', 'M', 'mL', 100, 100, '冷库A-1', 1)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    init_db(conn)
    init_db(conn)  # 重复初始化幂等

    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "location_transfer_reversals",
        "location_transfer_reversal_items",
    } <= tables

    from app.schemas import (
        LocationTransferRequest, LocationTransferItemIn,
        LocationTransferReversalRequest,
    )

    tr.create_transfer(conn, LocationTransferRequest(
        transfer_no="TR-M", operator="迁移人",
        items=[LocationTransferItemIn(
            sample_id="S1", from_location="冷库A-1", to_location="冷库B-1")],
    ))
    res = rev.create_reversal(conn, LocationTransferReversalRequest(
        reversal_no="RV-M", transfer_no="TR-M", operator="冲正人",
    ))
    assert res.items[0].to_location == "冷库A-1"
    got = rev.get_reversal(conn, "RV-M")
    assert got.transfer_no == "TR-M"
    hist = tr.get_history(conn, "S1")
    assert [h.kind for h in hist.history] == ["reversal", "transfer"]
    conn.close()
