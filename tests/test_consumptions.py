"""核销领用模块端到端测试。

覆盖:分次核销、单事务校验(预留未释放/逐项未核销量/样品余量)、整单回滚、
扣减余量与递增版本、不可变审计明细、幂等重放与冲突、核销后锁定量口径、
释放仅解除剩余锁定、余量账本一致、并发串行化与旧库自动建表。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import consumptions as con
from app import reservations as rsv
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
        c.db = mem  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sample(client, op_no, sid, amount, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _reserve(client, no, items, **kw):
    body = {"reservation_no": no, "items": items, **kw}
    r = client.post("/api/v1/reservations", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _consume(client, no, reservation_no, items, **kw):
    body = {"consumption_no": no, "reservation_no": reservation_no,
            "items": items, **kw}
    return client.post("/api/v1/consumptions", json=body)


def _availability(client, sid):
    r = client.get(f"/api/v1/reservations?sample_id={sid}")
    assert r.status_code == 200, r.text
    return r.json()["samples"][0]


# ---------------------------------------------------------------- 基本核销

def test_consume_deducts_balance_version_and_lock(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])

    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}],
                 operator="吴九", note="第一批领用")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayed"] is False
    assert body["status"] == "active"
    assert body["created_at"] is not None
    item = body["items"][0]
    assert item["amount"] == 10.0
    assert item["locked_amount"] == 30.0
    assert item["consumed_before"] == 0.0
    assert item["consumed_total"] == 10.0
    assert item["remaining_locked"] == 20.0
    assert item["remaining"] == 90.0          # 余量扣减
    assert item["reserved"] == 20.0           # 只把未核销部分计入锁定
    assert item["available"] == 70.0          # 核销不改变可用量
    assert (item["from_version"], item["to_version"]) == (0, 1)

    # 可用量查询同口径:余量 90、未核销锁定 20、可用 70
    assert _availability(client, "S1") == {
        "sample_id": "S1", "unit": "mL",
        "remaining": 90.0, "reserved": 20.0, "available": 70.0,
    }
    # 预留单明细带已核销/剩余锁定
    r = client.get("/api/v1/reservations/RSV-1")
    ri = r.json()["items"][0]
    assert ri["consumed"] == 10.0
    assert ri["remaining_locked"] == 20.0


def test_consume_multiple_items_in_one_transaction(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _reserve(client, "RSV-1", [
        {"sample_id": "S1", "amount": 30},
        {"sample_id": "S2", "amount": 20},
    ])
    r = _consume(client, "CV-1", "RSV-1", [
        {"sample_id": "S1", "amount": 10},
        {"sample_id": "S2", "amount": 5},
    ])
    assert r.status_code == 200, r.text
    items = {i["sample_id"]: i for i in r.json()["items"]}
    assert (items["S1"]["remaining"], items["S1"]["remaining_locked"]) == (90.0, 20.0)
    assert (items["S2"]["remaining"], items["S2"]["remaining_locked"]) == (45.0, 15.0)


def test_partial_consumptions_accumulate_until_exhausted(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])

    for idx, amount in enumerate((10, 15, 5), start=1):
        r = _consume(client, f"CV-{idx}", "RSV-1",
                     [{"sample_id": "S1", "amount": amount}])
        assert r.status_code == 200, r.text

    # 30 全部核销:剩余锁定归零,余量 70
    av = _availability(client, "S1")
    assert av == {"sample_id": "S1", "unit": "mL",
                  "remaining": 70.0, "reserved": 0.0, "available": 70.0}

    # 再核销哪怕 1 也超过未核销量
    r = _consume(client, "CV-9", "RSV-1", [{"sample_id": "S1", "amount": 1}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_unconsumed"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 1.0
    assert c["available"] == 0.0
    assert c["op_no"] == "CV-9"

    ri = client.get("/api/v1/reservations/RSV-1").json()["items"][0]
    assert ri["consumed"] == 30.0
    assert ri["remaining_locked"] == 0.0


# ---------------------------------------------------------------- 整单回滚

def test_any_item_failure_rolls_back_entire_consumption(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [
        {"sample_id": "S1", "amount": 30},
        {"sample_id": "S2", "amount": 10},
    ])
    # S1 合法;S2 未核销量只剩 10,请求 15 -> 整单拒绝
    r = _consume(client, "CV-BAD", "RSV-1", [
        {"sample_id": "S1", "amount": 30},
        {"sample_id": "S2", "amount": 15},
    ])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == "insufficient_unconsumed"
    assert conflicts[0]["sample_id"] == "S2"

    # 核销单与明细都不存在
    assert client.get("/api/v1/consumptions/CV-BAD").status_code == 404
    # S1/S2 余量、版本、锁定量全部未变
    assert _availability(client, "S1")["remaining"] == 100.0
    assert _availability(client, "S1")["reserved"] == 30.0
    assert _availability(client, "S2")["reserved"] == 10.0


def test_conflicts_collected_for_all_items_at_once(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 10}])
    r = _consume(client, "CV-BAD", "RSV-1", [
        {"sample_id": "S1", "amount": 99},   # 超未核销量
        {"sample_id": "S2", "amount": 1},    # 样品存在但不在预留单
    ])
    assert r.status_code == 409
    codes = {c["code"] for c in r.json()["conflicts"]}
    assert codes == {"insufficient_unconsumed", "reservation_item_not_found"}


def test_consume_sample_not_in_reservation_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 10}])
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S2", "amount": 1}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "reservation_item_not_found"
    assert c["sample_id"] == "S2"


def test_consume_unknown_sample_is_sample_not_found(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 10}])
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "GHOST", "amount": 1}])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_consume_released_reservation_conflicts(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    assert client.delete("/api/v1/reservations/RSV-1").status_code == 200
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 1}])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "reservation_closed"
    # 余量与版本未动
    assert _availability(client, "S1")["remaining"] == 100.0


def test_consume_unknown_reservation_404(client):
    r = _consume(client, "CV-1", "NOPE", [{"sample_id": "S1", "amount": 1}])
    assert r.status_code == 404
    assert r.json()["conflicts"][0]["code"] == "reservation_not_found"


def test_consume_insufficient_sample_balance(client):
    """余量被不感知预留的分装取出后可能低于锁定量,此时按样品余量拦截。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    # 分装 80 出去(分装只看余量):余量 20、锁定仍 30
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 80}],
    })
    assert r.status_code == 200, r.text

    # 未核销量 30 足够,但样品余量仅 20 -> 409 insufficient_balance,整单不变
    r = _consume(client, "CV-BAD", "RSV-1", [{"sample_id": "S1", "amount": 25}])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["requested"] == 25.0
    assert c["available"] == 20.0
    assert _availability(client, "S1")["remaining"] == 20.0

    # 20 刚好可核销:余量清零、锁定 30 -> 10
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 20}])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["remaining"] == 0.0
    assert item["remaining_locked"] == 10.0


def test_duplicate_sample_in_consumption_rejected(client):
    r = _consume(client, "CV-1", "RSV-1", [
        {"sample_id": "S1", "amount": 1},
        {"sample_id": "S1", "amount": 2},
    ])
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等

def test_consume_idempotent_replay_and_payload_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    body = {"consumption_no": "CV-IDEM", "reservation_no": "RSV-1",
            "operator": "吴九", "items": [{"sample_id": "S1", "amount": 10}]}

    r1 = client.post("/api/v1/consumptions", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    r2 = client.post("/api/v1/consumptions", json=body)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["replayed"] is True
    assert b2 == r1.json() | {"replayed": True}

    # 只回放、未再次扣减:余量 90、版本 1
    av = _availability(client, "S1")
    assert av["remaining"] == 90.0

    # 同号不同内容 -> 409
    r3 = client.post("/api/v1/consumptions", json={
        "consumption_no": "CV-IDEM", "reservation_no": "RSV-1",
        "items": [{"sample_id": "S1", "amount": 11}],
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "consumption_no_conflict"

    # 明细只有一条
    n = client.get("/api/v1/consumptions/CV-IDEM").json()
    assert len(n["items"]) == 1
    assert n["items"][0]["amount"] == 10.0


def test_replay_after_release_returns_original_result(client):
    """核销号已落库后预留被释放,同号重放仍返回首次结果且不再扣减。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    body = {"consumption_no": "CV-1", "reservation_no": "RSV-1",
            "items": [{"sample_id": "S1", "amount": 10}]}
    first = client.post("/api/v1/consumptions", json=body).json()
    assert client.delete("/api/v1/reservations/RSV-1").status_code == 200

    replay = client.post("/api/v1/consumptions", json=body).json()
    assert replay["replayed"] is True
    assert replay["items"] == first["items"]
    # 余量仍为首次核销后的 90
    assert _availability(client, "S1")["remaining"] == 90.0


# ---------------------------------------------------------------- 释放口径

def test_release_only_lifts_remaining_lock(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 20}])
    # 核销后:余量 80、未核销锁定 10、可用 70
    assert _availability(client, "S1") == {
        "sample_id": "S1", "unit": "mL",
        "remaining": 80.0, "reserved": 10.0, "available": 70.0,
    }

    r = client.delete("/api/v1/reservations/RSV-1")
    assert r.status_code == 200
    item = r.json()["items"][0]
    # 只解除剩余 10 的锁定;已核销 20 不归还
    assert item["consumed"] == 20.0
    assert item["remaining_locked"] == 10.0
    assert item["reserved"] == 0.0
    assert item["remaining"] == 80.0
    assert item["available"] == 80.0


def test_release_after_full_consumption_changes_nothing(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 30}])
    before = _availability(client, "S1")
    r = client.delete("/api/v1/reservations/RSV-1")
    assert r.status_code == 200
    after = _availability(client, "S1")
    assert before == after  # 已无锁定可解除


# ---------------------------------------------------------------- 账本 / 销毁联动

def test_balance_ledger_includes_consumptions(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 100)
    _reserve(client, "RSV-1", [
        {"sample_id": "S1", "amount": 30},
        {"sample_id": "S2", "amount": 20},
    ])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}])
    _consume(client, "CV-2", "RSV-1", [
        {"sample_id": "S1", "amount": 20},
        {"sample_id": "S2", "amount": 20},
    ])

    r = client.get("/api/v1/checks/balances")
    assert r.status_code == 200
    body = r.json()
    assert body["consistent"] is True
    bal = {b["sample_id"]: b for b in body["samples"]}
    assert bal["S1"]["stored_remaining"] == 70.0
    assert bal["S1"]["ledger_remaining"] == 70.0
    assert bal["S1"]["consumed"] == 30.0
    assert bal["S2"]["consumed"] == 20.0

    # 单样品校验同样一致,并暴露 consumed
    r = client.get("/api/v1/samples/S1/balance")
    assert r.json()["consistent"] is True
    assert r.json()["samples"][0]["consumed"] == 30.0


def test_dispose_checks_unconsumed_lock_after_consumption(client):
    """销毁按「余量 - 未核销锁定」校验;核销后锁定缩小,余量同步出库。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 60}])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 30}])
    # 核销后:余量 70、未核销锁定 30、可用 40
    # 销毁 41 -> 可用不足
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-1",
        "items": [{"sample_id": "S1", "amount": 41, "reason": "过期"}],
    })
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_available"
    assert c["available"] == 40.0
    # 销毁 40 恰好成功;账本仍一致
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-2",
        "items": [{"sample_id": "S1", "amount": 40, "reason": "过期"}],
    })
    assert r.status_code == 200, r.text
    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True
    b = next(x for x in bal["samples"] if x["sample_id"] == "S1")
    assert b["stored_remaining"] == 30.0  # 剩余的未核销锁定实物


# ---------------------------------------------------------------- 不可变审计

def test_consumption_records_are_immutable(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    r = _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}])
    assert r.status_code == 200

    # 通过依赖注入拿到同一内存连接做直连断言
    conn = client.db
    for sql in (
        "UPDATE consumption_items SET amount = 1 WHERE consumption_no = 'CV-1'",
        "DELETE FROM consumption_items WHERE consumption_no = 'CV-1'",
        "UPDATE consumptions SET note = 'x' WHERE consumption_no = 'CV-1'",
        "DELETE FROM consumptions WHERE consumption_no = 'CV-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_consumption_audit_rows_recorded(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}],
             operator="吴九")
    conn = client.db
    h = conn.execute(
        "SELECT * FROM consumptions WHERE consumption_no = 'CV-1'"
    ).fetchone()
    assert h["reservation_no"] == "RSV-1"
    assert h["operator"] == "吴九"
    i = conn.execute(
        "SELECT * FROM consumption_items WHERE consumption_no = 'CV-1'"
    ).fetchone()
    assert i["amount"] == 10.0
    assert i["from_version"] == 0 and i["to_version"] == 1
    assert i["remaining_after"] == 90.0


# ---------------------------------------------------------------- 查询

def test_get_consumption_returns_first_result(client):
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 30}])
    _consume(client, "CV-1", "RSV-1", [{"sample_id": "S1", "amount": 10}])
    r = client.get("/api/v1/consumptions/CV-1")
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is False
    assert body["reservation_no"] == "RSV-1"
    assert body["items"][0]["consumed_total"] == 10.0

    assert client.get("/api/v1/consumptions/NOPE").status_code == 404


# ---------------------------------------------------------------- 并发

def test_concurrent_consumptions_serialize_without_oversell(tmp_path):
    """两线程并发核销同一预留明细(各 20,共 40 > 未核销 30),恰一失败。"""
    db_path = str(tmp_path / "c.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    setup.execute(
        "INSERT INTO reservations (reservation_no, status, payload)"
        " VALUES ('RSV-1', 'active', '{}')"
    )
    setup.execute(
        "INSERT INTO reservation_items (reservation_no, sample_id, amount,"
        " unit, seq_index) VALUES ('RSV-1', 'S1', 30, 'mL', 0)"
    )
    setup.commit()
    setup.close()

    from app.schemas import ConsumptionRequest

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(no: str) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            con.create_consumption(c, ConsumptionRequest(
                consumption_no=no, reservation_no="RSV-1",
                items=[{"sample_id": "S1", "amount": 20}],  # type: ignore[arg-type]
            ))
            outcomes.append("ok")
        except con.ConsumptionError:
            outcomes.append("conflict")
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("CV-1",))
    t2 = threading.Thread(target=worker, args=("CV-2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    check = connect(db_path)
    consumed = check.execute(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM consumption_items"
        " WHERE reservation_no = 'RSV-1' AND sample_id = 'S1'"
    ).fetchone()["c"]
    remaining = check.execute(
        "SELECT remaining, version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert consumed == 20.0
    assert remaining["remaining"] == 80.0
    assert remaining["version"] == 1
    check.close()


# ---------------------------------------------------------------- 旧库建表

def test_tables_created_on_old_database(tmp_path):
    """旧版本数据库(无核销表)启动初始化后自动补齐,迁移幂等。"""
    db_path = str(tmp_path / "old.db")
    conn = connect(db_path)
    # 只建基础表,模拟旧库
    conn.executescript(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " version INTEGER NOT NULL DEFAULT 0, location TEXT, is_root INTEGER NOT NULL"
        " DEFAULT 0, created_at TEXT);"
        "CREATE TABLE reservations (reservation_no TEXT PRIMARY KEY, status TEXT NOT"
        " NULL, payload TEXT NOT NULL, note TEXT, created_at TEXT, released_at TEXT);"
        "CREATE TABLE reservation_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " reservation_no TEXT NOT NULL, sample_id TEXT NOT NULL, amount REAL NOT NULL,"
        " unit TEXT NOT NULL, seq_index INTEGER NOT NULL);"
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT);"
    )
    conn.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    init_db(conn)
    init_db(conn)  # 幂等:再跑一次不报错
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        ).fetchall()
    }
    assert {"consumptions", "consumption_items"} <= names
    assert {
        "trg_consumption_no_update", "trg_consumption_no_delete",
        "trg_citem_no_update", "trg_citem_no_delete",
    } <= names

    # 补齐后功能正常
    from app.schemas import ReservationRequest, ConsumptionRequest

    rsv.create_reservation(conn, ReservationRequest(
        reservation_no="RSV-1",
        items=[{"sample_id": "S1", "amount": 10}],  # type: ignore[arg-type]
    ))
    result = con.create_consumption(conn, ConsumptionRequest(
        consumption_no="CV-1", reservation_no="RSV-1",
        items=[{"sample_id": "S1", "amount": 4}],  # type: ignore[arg-type]
    ))
    assert result.items[0].remaining_locked == 6.0
    conn.close()
