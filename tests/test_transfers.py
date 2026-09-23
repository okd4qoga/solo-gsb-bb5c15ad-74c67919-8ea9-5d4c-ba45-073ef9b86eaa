"""位置交接模块端到端测试:

- 一次提交多个样品的原位置/目标位置,单事务统一更新并写审计
- 任一样品不存在或已离开原位置则整批回滚、冲突一次列全
- 交接人/时间/变更前后位置逐项留痕;按交接单号幂等重放、同号不同内容冲突
- 按样品查询交接历史(最新在前)、样品不存在返回 404
- 交接不改动余量与账本;审计表触发器禁止 UPDATE/DELETE
- 并发交接同一组样品时由 BEGIN IMMEDIATE 串行化,后者按最新位置校验失败
- 旧版本数据库启动后自动建表(位置交接表随初始化幂等创建)
"""
from __future__ import annotations

import threading

import pytest
import sqlite3
from fastapi.testclient import TestClient

from app import transfers as tr
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


def _transfer(client, no="TR-1", operator="王交接", items=None, **extra):
    if items is None:
        items = [
            {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
            {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
        ]
    return client.post("/api/v1/transfers", json={
        "transfer_no": no, "operator": operator, "items": items, **extra,
    })


def _locations(client):
    return {
        x["sample_id"]: x["location"]
        for x in client.get("/api/v1/samples").json()["items"]
    }


# ---------------------------------------------------------------- 成功路径

def test_transfer_moves_multiple_samples_and_records_audit(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", 50, location="冷库A-2")

    r = _transfer(client, note="冷库迁移")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transfer_no"] == "TR-1"
    assert body["operator"] == "王交接"
    assert body["note"] == "冷库迁移"
    assert body["replayed"] is False
    assert body["handover_at"] == body["created_at"]
    assert body["items"] == [
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
    ]

    # 样品位置统一更新
    assert _locations(client) == {"S1": "冷库B-1", "S2": "冷库B-2"}


def test_transfer_does_not_change_remaining_and_ledger_stays_consistent(client):
    _sample(client, "OP-1", "S1", 100, location="冷库A-1")
    assert _transfer(client, items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ]).status_code == 200

    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True
    s1 = next(b for b in bal["samples"] if b["sample_id"] == "S1")
    assert s1["stored_remaining"] == 100.0
    assert s1["ledger_remaining"] == 100.0


def test_transfer_from_unregistered_location_using_null(client):
    # 取样时未登记位置(location 为 null),用 null 作为原位置可以交接
    _sample(client, "OP-1", "S1")
    assert _locations(client)["S1"] is None
    r = _transfer(client, items=[
        {"sample_id": "S1", "from_location": None, "to_location": "冷库B-1"},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["from_location"] is None
    assert _locations(client)["S1"] == "冷库B-1"


# ---------------------------------------------------------------- 校验与整批回滚

def test_transfer_location_mismatch_rolls_back_whole_batch(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", location="冷库A-2")

    # S1 原位置正确,S2 声称的原位置已过期(实际仍会被后续操作移动的场景见下)
    client.post("/api/v1/transfers", json={
        "transfer_no": "TR-PRE", "operator": "赵六",
        "items": [{"sample_id": "S2", "from_location": "冷库A-2",
                   "to_location": "冷库Z-9"}],
    })

    r = _transfer(client, no="TR-BAD")
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "location_mismatch"
    assert c["sample_id"] == "S2"
    assert c["op_no"] == "TR-BAD"
    assert c["requested_location"] == "冷库A-2"
    assert c["available_location"] == "冷库Z-9"
    assert "冷库Z-9" in c["message"] and "冷库A-2" in c["message"]

    # 整批不变:S1 也没有被移动;失败的交接单不留痕
    assert _locations(client) == {"S1": "冷库A-1", "S2": "冷库Z-9"}
    assert client.get("/api/v1/samples/S1/transfers").json()["history"] == []
    assert client.get("/api/v1/samples/S2/transfers").json()["history"][0]["transfer_no"] == "TR-PRE"


def test_transfer_collects_all_conflicts_in_one_response(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    # S2 不存在;S1 原位置也说错
    r = _transfer(client, no="TR-MULTI", items=[
        {"sample_id": "S1", "from_location": "冷库X", "to_location": "冷库B-1"},
        {"sample_id": "GHOST", "from_location": "冷库A-2", "to_location": "冷库B-2"},
    ])
    assert r.status_code == 409
    codes = sorted(c["code"] for c in r.json()["conflicts"])
    assert codes == ["location_mismatch", "sample_not_found"]
    # 整批不变
    assert _locations(client) == {"S1": "冷库A-1"}


def test_transfer_unknown_sample_is_409_and_changes_nothing(client):
    r = _transfer(client, no="TR-NOPE", items=[
        {"sample_id": "GHOST", "from_location": None, "to_location": "冷库B-1"},
    ])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_transfer_rejects_duplicate_sample_in_request(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    r = _transfer(client, items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-2"},
    ])
    assert r.status_code == 422


def test_transfer_requires_nonempty_operator_and_destination(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    r = client.post("/api/v1/transfers", json={
        "transfer_no": "TR-X", "operator": "",
        "items": [{"sample_id": "S1", "from_location": "冷库A-1",
                   "to_location": "冷库B-1"}],
    })
    assert r.status_code == 422
    r = client.post("/api/v1/transfers", json={
        "transfer_no": "TR-Y", "operator": "王交接",
        "items": [{"sample_id": "S1", "from_location": "冷库A-1",
                   "to_location": ""}],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等

def test_transfer_idempotent_replay_returns_original_result(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    body = {
        "transfer_no": "TR-ID", "operator": "王交接", "note": "迁移",
        "items": [{"sample_id": "S1", "from_location": "冷库A-1",
                   "to_location": "冷库B-1"}],
    }
    r1 = client.post("/api/v1/transfers", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False
    first_time = r1.json()["handover_at"]

    # 交接后样品已不在原位置;同单号同内容重放仍返回首次结果,且不会再次移动
    r2 = client.post("/api/v1/transfers", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["handover_at"] == first_time
    assert _locations(client)["S1"] == "冷库B-1"

    # 只写过一次审计明细
    conn = next(app.dependency_overrides[get_conn]())
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM location_transfer_items WHERE transfer_no = 'TR-ID'"
    ).fetchone()["c"]
    assert n == 1


def test_transfer_same_no_different_payload_conflicts(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    body1 = {
        "transfer_no": "TR-C", "operator": "王交接",
        "items": [{"sample_id": "S1", "from_location": "冷库A-1",
                   "to_location": "冷库B-1"}],
    }
    assert client.post("/api/v1/transfers", json=body1).status_code == 200
    body2 = {
        "transfer_no": "TR-C", "operator": "王交接",
        "items": [{"sample_id": "S1", "from_location": "冷库A-1",
                   "to_location": "冷库X"}],
    }
    r = client.post("/api/v1/transfers", json=body2)
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "transfer_no_conflict"


# ---------------------------------------------------------------- 历史

def test_history_returns_records_newest_first_with_operator_and_locations(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _transfer(client, no="TR-1", operator="王交接", items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ])
    _transfer(client, no="TR-2", operator="李接收", note="二次搬运", items=[
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库C-1"},
    ])

    r = client.get("/api/v1/samples/S1/transfers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == "S1"
    assert body["current_location"] == "冷库C-1"
    assert [h["transfer_no"] for h in body["history"]] == ["TR-2", "TR-1"]
    first = body["history"][0]
    assert first["operator"] == "李接收"
    assert first["note"] == "二次搬运"
    assert first["from_location"] == "冷库B-1"
    assert first["to_location"] == "冷库C-1"
    assert first["handover_at"]


def test_history_for_sample_without_transfers_is_empty(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    body = client.get("/api/v1/samples/S1/transfers").json()
    assert body["history"] == []
    assert body["current_location"] == "冷库A-1"


def test_history_unknown_sample_returns_404(client):
    r = client.get("/api/v1/samples/GHOST/transfers")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


# ---------------------------------------------------------------- 审计不可变

def test_transfer_audit_tables_are_immutable(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    assert _transfer(client, items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    ]).status_code == 200

    conn = next(app.dependency_overrides[get_conn]())
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE location_transfer_items SET to_location = 'z'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfer_items")
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE location_transfers SET operator = 'z'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfers")


# ---------------------------------------------------------------- 并发

def test_concurrent_transfers_serialize_and_loser_sees_stale_origin(tmp_path):
    """两笔并发交接都声称 S1 在「冷库A-1」:BEGIN IMMEDIATE 串行化,
    先拿到写锁者把样品移走,后者按最新位置校验失败、整批不变。"""
    db_path = str(tmp_path / "t.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, version, location, is_root) VALUES"
        " ('S1', 'M', 'mL', 100, 100, 0, '冷库A-1', 1)"
    )
    setup.commit()
    setup.close()

    from app.schemas import LocationTransferRequest, LocationTransferItemIn

    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(2)

    def worker(no: str, dest: str) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            tr.create_transfer(c, LocationTransferRequest(
                transfer_no=no,
                operator=f"人-{no}",
                items=[LocationTransferItemIn(
                    sample_id="S1", from_location="冷库A-1", to_location=dest)],
            ))
            winners.append(no)
        except tr.TransferError as exc:
            losers.append((no, exc.conflicts[0].code))
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("TR-C1", "冷库B-1"))
    t2 = threading.Thread(target=worker, args=("TR-C2", "冷库C-1"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 恰好一成一败;败者冲突码必为 location_mismatch(实际位置已是胜者目标)
    assert len(winners) == 1 and len(losers) == 1
    loser_no, loser_code = losers[0]
    assert loser_no != winners[0]
    assert loser_code == "location_mismatch"

    check = connect(db_path)
    winner_dest = "冷库B-1" if winners[0] == "TR-C1" else "冷库C-1"
    assert check.execute(
        "SELECT location FROM samples WHERE sample_id = 'S1'"
    ).fetchone()["location"] == winner_dest
    # 只有成功的那笔留下审计
    nos = [
        r["transfer_no"]
        for r in check.execute(
            "SELECT transfer_no FROM location_transfer_items WHERE sample_id = 'S1'"
        ).fetchall()
    ]
    assert nos == winners
    check.close()


# ---------------------------------------------------------------- 旧库迁移

def test_init_db_creates_transfer_tables_on_legacy_database(tmp_path):
    """旧版本数据库(仅有老表)启动初始化后,位置交接表自动补齐且全流程可用。"""
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "legacy.db")
    raw = _sqlite3.connect(db_path)
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
    init_db(conn)  # 幂等建表,不应报错
    init_db(conn)  # 再执行一次也不重复创建

    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"location_transfers", "location_transfer_items"} <= tables

    from app.schemas import LocationTransferRequest, LocationTransferItemIn

    res = tr.create_transfer(conn, LocationTransferRequest(
        transfer_no="TR-M", operator="迁移人",
        items=[LocationTransferItemIn(
            sample_id="S1", from_location="冷库A-1", to_location="冷库B-1")],
    ))
    assert res.items[0].to_location == "冷库B-1"
    hist = tr.get_history(conn, "S1")
    assert len(hist.history) == 1
    assert hist.current_location == "冷库B-1"
    conn.close()
