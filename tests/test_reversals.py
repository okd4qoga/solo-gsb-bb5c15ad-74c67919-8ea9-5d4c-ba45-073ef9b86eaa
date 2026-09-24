"""位置交接整单冲正端到端测试:

- 按原交接单号/冲正号/操作人提交,读取原单全部明细整单回退到原位置(含 null)
- 单事务校验:每个样品最后一次交接仍是原单明细且当前位置等于原单目标位置,
  任一不符整单不改、冲突一次列全
- 冲正审计不可变并接入位置历史,原交接记录保留;不改动余量与账本
- 幂等:同冲正号同内容重放不重复回退;同号异内容 409;原单重复冲正 409
- 按冲正号查询;冲正号/原单号不存在返回 404
- 并发冲正同一原单由 BEGIN IMMEDIATE 串行化,仅一笔成功
- 旧版本数据库启动后自动建表(冲正表随初始化幂等创建)
"""
from __future__ import annotations

import threading

import pytest
import sqlite3
from fastapi.testclient import TestClient

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


def _transfer(client, no="TR-1", operator="王交接", items=None, **extra):
    if items is None:
        items = [
            {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
            {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
        ]
    return client.post("/api/v1/transfers", json={
        "transfer_no": no, "operator": operator, "items": items, **extra,
    })


def _reverse(client, reversal_no="RV-1", transfer_no="TR-1", operator="张冲正",
             **extra):
    return client.post("/api/v1/transfers/reversals", json={
        "reversal_no": reversal_no, "transfer_no": transfer_no,
        "operator": operator, **extra,
    })


def _locations(client):
    return {
        x["sample_id"]: x["location"]
        for x in client.get("/api/v1/samples").json()["items"]
    }


def _seed_two_transferred(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", 50, location="冷库A-2")
    r = _transfer(client, note="冷库迁移")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 成功路径

def test_reversal_restores_all_samples_to_original_locations(client):
    _seed_two_transferred(client)
    assert _locations(client) == {"S1": "冷库B-1", "S2": "冷库B-2"}

    r = _reverse(client, note="搬迁取消")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversal_no"] == "RV-1"
    assert body["transfer_no"] == "TR-1"
    assert body["operator"] == "张冲正"
    assert body["note"] == "搬迁取消"
    assert body["replayed"] is False
    assert body["reversed_at"] == body["created_at"]
    assert body["items"] == [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库A-1"},
        {"sample_id": "S2", "from_location": "冷库B-2", "to_location": "冷库A-2"},
    ]

    # 全部样品恢复到原单原位置
    assert _locations(client) == {"S1": "冷库A-1", "S2": "冷库A-2"}


def test_reversal_restores_unregistered_null_location(client):
    # 原单把未登记位置(null)的样品交接出去,冲正后恢复为 null
    _sample(client, "OP-1", "S1")
    assert _locations(client)["S1"] is None
    assert _transfer(client, items=[
        {"sample_id": "S1", "from_location": None, "to_location": "冷库B-1"},
    ]).status_code == 200

    r = _reverse(client)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["from_location"] == "冷库B-1"
    assert r.json()["items"][0]["to_location"] is None
    assert _locations(client)["S1"] is None


def test_reversal_keeps_original_transfer_records(client):
    _seed_two_transferred(client)
    assert _reverse(client).status_code == 200

    # 原交接记录保留:原单号同内容重放仍返回首次交接结果
    r = _transfer(client, note="冷库迁移")
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["items"][0]["to_location"] == "冷库B-1"

    conn = next(app.dependency_overrides[get_conn]())
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM location_transfer_items WHERE transfer_no = 'TR-1'"
    ).fetchone()["c"] == 2


def test_reversal_integrates_into_location_history(client):
    _seed_two_transferred(client)
    assert _reverse(client, note="搬迁取消").status_code == 200

    body = client.get("/api/v1/samples/S1/transfers").json()
    assert body["current_location"] == "冷库A-1"
    assert [h["kind"] for h in body["history"]] == ["reversal", "transfer"]
    first = body["history"][0]
    assert first["reversal_no"] == "RV-1"
    assert first["transfer_no"] == "TR-1"  # 指向被冲正的原交接单
    assert first["operator"] == "张冲正"
    assert first["note"] == "搬迁取消"
    assert first["from_location"] == "冷库B-1"
    assert first["to_location"] == "冷库A-1"
    assert first["handover_at"]
    # 原交接记录仍在历史中
    second = body["history"][1]
    assert second["transfer_no"] == "TR-1"
    assert second["reversal_no"] is None
    assert second["to_location"] == "冷库B-1"


def test_reversal_does_not_change_remaining_and_ledger_stays_consistent(client):
    _seed_two_transferred(client)
    assert _reverse(client).status_code == 200

    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True
    s1 = next(b for b in bal["samples"] if b["sample_id"] == "S1")
    assert s1["stored_remaining"] == 100.0
    assert s1["ledger_remaining"] == 100.0


def test_sample_can_be_transferred_again_after_reversal(client):
    _seed_two_transferred(client)
    assert _reverse(client).status_code == 200

    # 冲正后样品回到原位置,可以从该位置再次交接,且新交接也可被冲正
    assert _transfer(client, no="TR-2", operator="李接收", items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库C-1"},
    ]).status_code == 200
    assert _reverse(client, reversal_no="RV-2", transfer_no="TR-2").status_code == 200
    assert _locations(client)["S1"] == "冷库A-1"
    kinds = [
        (h["kind"], h.get("reversal_no") or h["transfer_no"])
        for h in client.get("/api/v1/samples/S1/transfers").json()["history"]
    ]
    assert kinds == [
        ("reversal", "RV-2"), ("transfer", "TR-2"),
        ("reversal", "RV-1"), ("transfer", "TR-1"),
    ]


# ---------------------------------------------------------------- 校验与整单不改

def test_reversal_fails_when_sample_moved_by_later_transfer(client):
    _seed_two_transferred(client)
    # S2 此后又被另一张交接单移动
    _transfer(client, no="TR-2", operator="李接收", items=[
        {"sample_id": "S2", "from_location": "冷库B-2", "to_location": "冷库Z-9"},
    ])

    r = _reverse(client, reversal_no="RV-BAD")
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "last_transfer_mismatch"
    assert c["sample_id"] == "S2"
    assert c["op_no"] == "RV-BAD"
    assert "TR-2" in c["message"] and "TR-1" in c["message"]

    # 整单不改:S1 也没有被回退;失败的冲正不留痕
    assert _locations(client) == {"S1": "冷库B-1", "S2": "冷库Z-9"}
    hist = client.get("/api/v1/samples/S1/transfers").json()["history"]
    assert [h["kind"] for h in hist] == ["transfer"]
    assert client.get("/api/v1/transfers/reversals/RV-BAD").status_code == 404


def test_reversal_collects_all_conflicts_in_one_response(client):
    _sample(client, "OP-1", "S1", location="冷库A-1")
    _sample(client, "OP-2", "S2", location="冷库A-2")
    _sample(client, "OP-3", "S3", location="冷库A-3")
    _transfer(client, items=[
        {"sample_id": "S1", "from_location": "冷库A-1", "to_location": "冷库B-1"},
        {"sample_id": "S2", "from_location": "冷库A-2", "to_location": "冷库B-2"},
        {"sample_id": "S3", "from_location": "冷库A-3", "to_location": "冷库B-3"},
    ])
    # S1 被后续交接移动(最后一次交接不再是原单明细);
    # S2 先被移走又被移回原目标位置,但最后一次交接仍不是原单明细
    _transfer(client, no="TR-2", items=[
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库Z-1"},
    ])
    _transfer(client, no="TR-3", items=[
        {"sample_id": "S2", "from_location": "冷库B-2", "to_location": "冷库Z-2"},
    ])
    _transfer(client, no="TR-4", items=[
        {"sample_id": "S2", "from_location": "冷库Z-2", "to_location": "冷库B-2"},
    ])

    r = _reverse(client, reversal_no="RV-MULTI")
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert [(c["sample_id"], c["code"]) for c in conflicts] == [
        ("S1", "last_transfer_mismatch"),
        ("S2", "last_transfer_mismatch"),
    ]
    # 整单不改:S3 满足条件也不回退
    assert _locations(client) == {
        "S1": "冷库Z-1", "S2": "冷库B-2", "S3": "冷库B-3",
    }


def test_reversal_of_already_reversed_transfer_conflicts(client):
    _seed_two_transferred(client)
    assert _reverse(client).status_code == 200

    # 换一个冲正号对同一原单重复冲正
    r = _reverse(client, reversal_no="RV-2")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "transfer_already_reversed"
    assert "RV-1" in c["message"]
    # 位置保持第一次冲正后的状态
    assert _locations(client) == {"S1": "冷库A-1", "S2": "冷库A-2"}


def test_reversal_unknown_transfer_returns_404(client):
    r = _reverse(client, transfer_no="TR-GHOST")
    assert r.status_code == 404
    assert r.json()["error"] == "transfer_not_found"
    assert r.json()["conflicts"][0]["code"] == "transfer_not_found"


def test_reversal_requires_nonempty_reversal_no_and_operator(client):
    _seed_two_transferred(client)
    r = _reverse(client, reversal_no="")
    assert r.status_code == 422
    r = _reverse(client, operator="")
    assert r.status_code == 422
    r = _reverse(client, transfer_no="")
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等

def test_reversal_idempotent_replay_returns_original_result(client):
    _seed_two_transferred(client)
    body = {
        "reversal_no": "RV-ID", "transfer_no": "TR-1",
        "operator": "张冲正", "note": "搬迁取消",
    }
    r1 = client.post("/api/v1/transfers/reversals", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False
    first_time = r1.json()["reversed_at"]

    # 冲正后样品已不在原单目标位置;同号同内容重放仍返回首次结果,不重复回退
    r2 = client.post("/api/v1/transfers/reversals", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["reversed_at"] == first_time
    assert _locations(client) == {"S1": "冷库A-1", "S2": "冷库A-2"}

    # 只写过一次冲正审计明细
    conn = next(app.dependency_overrides[get_conn]())
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM location_transfer_reversal_items"
        " WHERE reversal_no = 'RV-ID'"
    ).fetchone()["c"]
    assert n == 2


def test_reversal_same_no_different_payload_conflicts(client):
    _seed_two_transferred(client)
    _transfer(client, no="TR-2", operator="李接收", items=[
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库C-1"},
    ])
    assert _reverse(client, reversal_no="RV-C", transfer_no="TR-2",
                    ).status_code == 200
    # 同号异内容(指向另一张原单)
    r = _reverse(client, reversal_no="RV-C", transfer_no="TR-1")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "reversal_no_conflict"
    # 同号异内容(仅操作人不同)
    r = _reverse(client, reversal_no="RV-C", transfer_no="TR-2", operator="别人")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "reversal_no_conflict"


# ---------------------------------------------------------------- 查询

def test_get_reversal_returns_first_result(client):
    _seed_two_transferred(client)
    assert _reverse(client, note="搬迁取消").status_code == 200

    r = client.get("/api/v1/transfers/reversals/RV-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversal_no"] == "RV-1"
    assert body["transfer_no"] == "TR-1"
    assert body["operator"] == "张冲正"
    assert body["note"] == "搬迁取消"
    assert body["replayed"] is False
    assert body["items"] == [
        {"sample_id": "S1", "from_location": "冷库B-1", "to_location": "冷库A-1"},
        {"sample_id": "S2", "from_location": "冷库B-2", "to_location": "冷库A-2"},
    ]


def test_get_reversal_unknown_no_returns_404(client):
    r = client.get("/api/v1/transfers/reversals/RV-GHOST")
    assert r.status_code == 404
    assert r.json()["error"] == "reversal_not_found"


# ---------------------------------------------------------------- 审计不可变

def test_reversal_audit_tables_are_immutable(client):
    _seed_two_transferred(client)
    assert _reverse(client).status_code == 200

    conn = next(app.dependency_overrides[get_conn]())
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "UPDATE location_transfer_reversal_items SET to_location = 'z'"
        )
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfer_reversal_items")
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE location_transfer_reversals SET operator = 'z'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM location_transfer_reversals")


# ---------------------------------------------------------------- 并发

def test_concurrent_reversals_of_same_transfer_serialize(tmp_path):
    """两笔并发冲正(不同冲正号)针对同一原单:BEGIN IMMEDIATE 串行化,
    恰好一笔成功回退,另一笔看到 transfer_already_reversed,位置只回退一次。"""
    db_path = str(tmp_path / "r.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, version, location, is_root) VALUES"
        " ('S1', 'M', 'mL', 100, 100, 0, '冷库B-1', 1)"
    )
    setup.execute(
        "INSERT INTO location_transfers (transfer_no, operator, payload)"
        " VALUES ('TR-1', '王交接', '{}')"
    )
    setup.execute(
        "INSERT INTO location_transfer_items (transfer_no, sample_id, seq_index,"
        " from_location, to_location, operator) VALUES"
        " ('TR-1', 'S1', 0, '冷库A-1', '冷库B-1', '王交接')"
    )
    setup.commit()
    setup.close()

    from app.schemas import LocationTransferReversalRequest

    winners: list[str] = []
    losers: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def worker(no: str) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            rev.create_reversal(c, LocationTransferReversalRequest(
                reversal_no=no, transfer_no="TR-1", operator=f"人-{no}",
            ))
            winners.append(no)
        except rev.ReversalError as exc:
            losers.append((no, exc.conflicts[0].code))
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("RV-C1",))
    t2 = threading.Thread(target=worker, args=("RV-C2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 恰好一成一败;败者冲突码必为 transfer_already_reversed
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0][0] != winners[0]
    assert losers[0][1] == "transfer_already_reversed"

    check = connect(db_path)
    # 位置只回退一次到原位置
    assert check.execute(
        "SELECT location FROM samples WHERE sample_id = 'S1'"
    ).fetchone()["location"] == "冷库A-1"
    # 只有成功的那笔留下冲正审计
    nos = [
        r["reversal_no"]
        for r in check.execute("SELECT reversal_no FROM location_transfer_reversals")
    ]
    assert nos == winners
    check.close()


# ---------------------------------------------------------------- 旧库迁移

def test_init_db_creates_reversal_tables_on_legacy_database(tmp_path):
    """旧版本数据库(仅有交接表)启动初始化后,冲正表自动补齐且全流程可用。"""
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
        " ('S1', 'M', 'mL', 100, 100, '冷库B-1', 1)"
    )
    raw.execute(
        "CREATE TABLE location_transfers (transfer_no TEXT PRIMARY KEY,"
        " operator TEXT NOT NULL, payload TEXT NOT NULL, note TEXT,"
        " created_at TEXT NOT NULL DEFAULT"
        " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    raw.execute(
        "CREATE TABLE location_transfer_items ("
        " item_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " transfer_no TEXT NOT NULL, sample_id TEXT NOT NULL,"
        " seq_index INTEGER NOT NULL, from_location TEXT, to_location TEXT NOT NULL,"
        " operator TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT"
        " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    raw.execute(
        "INSERT INTO location_transfers (transfer_no, operator, payload)"
        " VALUES ('TR-1', '王交接', '{}')"
    )
    raw.execute(
        "INSERT INTO location_transfer_items (transfer_no, sample_id, seq_index,"
        " from_location, to_location, operator) VALUES"
        " ('TR-1', 'S1', 0, '冷库A-1', '冷库B-1', '王交接')"
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
    assert {"location_transfer_reversals",
            "location_transfer_reversal_items"} <= tables

    from app.schemas import LocationTransferReversalRequest

    res = rev.create_reversal(conn, LocationTransferReversalRequest(
        reversal_no="RV-M", transfer_no="TR-1", operator="冲正人",
    ))
    assert res.items[0].to_location == "冷库A-1"
    hist = __import__("app.transfers", fromlist=["get_history"]).get_history(
        conn, "S1"
    )
    assert hist.current_location == "冷库A-1"
    assert [h.kind for h in hist.history] == ["reversal", "transfer"]
    conn.close()
