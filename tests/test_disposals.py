"""销毁/报废模块端到端测试:

- 多明细单事务销毁,按可用量(余量-未释放预留)逐项校验
- 任一不足整体回滚并返回逐项冲突;不可变销毁审计与出库流水
- 样品版本递增、余量账本仍一致、操作号幂等重放
- 批量提交(/batch 混合 dispose)与 dry-run(/validate、/batch/validate)
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import service
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


def _sample(client, op_no, sid, amount, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 基本销毁

def test_dispose_multiple_samples_and_persist_audit(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)

    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose",
        "op_no": "DSP-1",
        "operator": "李四",
        "location": "危废暂存柜",
        "items": [
            {"sample_id": "S1", "amount": 30, "reason": "污染"},
            {"sample_id": "S2", "amount": 50, "reason": "过期报废"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "dispose"
    assert body["samples_created"] == []
    assert body["edges"] == []
    assert body["replayed"] is False
    assert body["operator"] == "李四"
    assert body["remaining"] == {"S1": 70.0, "S2": 0.0}
    disposed = {d["sample_id"]: d for d in body["disposed"]}
    assert disposed["S1"]["amount"] == 30.0
    assert disposed["S1"]["unit"] == "mL"
    assert disposed["S1"]["reason"] == "污染"
    assert disposed["S1"]["remaining"] == 70.0
    assert disposed["S1"]["reserved"] == 0.0
    assert disposed["S1"]["available"] == 70.0
    assert disposed["S1"]["from_version"] == 0
    assert disposed["S1"]["to_version"] == 1
    d2 = disposed["S2"]
    assert d2["remaining"] == 0.0
    assert d2["from_version"] == 0 and d2["to_version"] == 1

    # 出库流水与不可变审计记录已写,且余量账本仍一致
    bal = client.get("/api/v1/checks/balances").json()
    assert bal["consistent"] is True
    by_id = {b["sample_id"]: b for b in bal["samples"]}
    assert by_id["S1"]["total_out"] == 30.0
    assert by_id["S2"]["total_out"] == 50.0
    assert by_id["S1"]["available"] == 70.0

    op = client.get("/api/v1/operations/DSP-1").json()
    assert op["type"] == "dispose"
    assert len(op["effect"]["disposed"]) == 2


def test_dispose_reasons_required_and_duplicate_sample_rejected(client):
    _sample(client, "OP-1", "S1", 10)
    # reason 必填
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-X",
        "items": [{"sample_id": "S1", "amount": 1}],
    })
    assert r.status_code == 422
    # items 中样品重复
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-X",
        "items": [
            {"sample_id": "S1", "amount": 1, "reason": "a"},
            {"sample_id": "S1", "amount": 1, "reason": "b"},
        ],
    })
    assert r.status_code == 422
    # amount 必须为正
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-X",
        "items": [{"sample_id": "S1", "amount": 0, "reason": "a"}],
    })
    assert r.status_code == 422


def test_dispose_audit_records_are_immutable(client):
    _sample(client, "OP-1", "S1", 10)
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-1",
        "items": [{"sample_id": "S1", "amount": 4, "reason": "污染"}],
    })
    assert r.status_code == 200, r.text

    # 直接拿到底层连接验证触发器禁止 UPDATE / DELETE
    conn = next(app.dependency_overrides[get_conn]())
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE dispose_records SET reason = 'x'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM dispose_records")


# ---------------------------------------------------------------- 可用量校验

def test_dispose_checks_available_against_reservations_and_rolls_back(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    # 锁定 S1 80、S2 10
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-1",
        "items": [
            {"sample_id": "S1", "amount": 80},
            {"sample_id": "S2", "amount": 10},
        ],
    })
    assert r.status_code == 200, r.text

    # S1 可销毁 20 但请求 30;S2 请求 45(可用 40)也不足
    # —— 两条冲突应一次全部返回
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-BAD",
        "items": [
            {"sample_id": "S1", "amount": 30, "reason": "污染"},
            {"sample_id": "S2", "amount": 45, "reason": "过期"},
        ],
    })
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert {c["code"] for c in conflicts} == {"insufficient_available"}
    by_sample = {c["sample_id"]: c for c in conflicts}
    assert by_sample["S1"]["requested"] == 30.0
    assert by_sample["S1"]["available"] == 20.0
    assert by_sample["S2"]["available"] == 40.0

    # 整体回滚:余量未变、操作号不存在、无审计记录
    bal = {b["sample_id"]: b for b in client.get(
        "/api/v1/checks/balances").json()["samples"]}
    assert bal["S1"]["stored_remaining"] == 100.0
    assert bal["S2"]["stored_remaining"] == 50.0
    assert client.get("/api/v1/operations/DSP-BAD").status_code == 404

    # 不存在的样品也逐项报 sample_not_found
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-GHOST",
        "items": [
            {"sample_id": "S1", "amount": 1, "reason": "污染"},
            {"sample_id": "GHOST", "amount": 1, "reason": "过期"},
        ],
    })
    assert r.status_code == 409
    codes = {c["sample_id"]: c["code"] for c in r.json()["conflicts"]}
    assert codes["GHOST"] == "sample_not_found"

    # 销毁不超过可用量(余量之内但预留之外)即可成功
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-OK",
        "items": [{"sample_id": "S1", "amount": 20, "reason": "污染"}],
    })
    assert r.status_code == 200, r.text
    d = r.json()["disposed"][0]
    assert d["remaining"] == 80.0
    assert d["reserved"] == 80.0
    assert d["available"] == 0.0
    # 预留仍生效:此时可用量为 0,再销毁 1 失败
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-NOPE",
        "items": [{"sample_id": "S1", "amount": 1, "reason": "污染"}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "insufficient_available"


# ---------------------------------------------------------------- 幂等

def test_dispose_idempotent_replay_and_payload_conflict(client):
    _sample(client, "OP-1", "S1", 10)
    body = {
        "type": "dispose", "op_no": "DSP-1",
        "items": [{"sample_id": "S1", "amount": 3, "reason": "污染"}],
    }
    r = client.post("/api/v1/operations/dispose", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is False

    # 同请求体重放:不再出库,余量不变
    r = client.post("/api/v1/operations/dispose", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["remaining"] == {"S1": 7.0}
    bal = client.get("/api/v1/samples/S1/balance").json()
    assert bal["samples"][0]["total_out"] == 3.0

    # 同号不同内容 409
    r = client.post("/api/v1/operations/dispose", json={
        "type": "dispose", "op_no": "DSP-1",
        "items": [{"sample_id": "S1", "amount": 4, "reason": "污染"}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "op_no_conflict"


# ---------------------------------------------------------------- 批量与 dry-run

def test_dispose_in_batch_and_batch_validate(client):
    _sample(client, "OP-1", "S1", 10)

    # dispose 与其他类型混在同一批次
    r = client.post("/api/v1/batch", json={"batch_id": "B-DSP", "operations": [
        {"type": "aliquot", "op_no": "OP-A", "parent_id": "S1",
         "outputs": [{"sample_id": "S1a", "amount": 4}]},
        {"type": "dispose", "op_no": "DSP-B1",
         "items": [
             {"sample_id": "S1", "amount": 2, "reason": "污染"},
             {"sample_id": "S1a", "amount": 1, "reason": "管裂报废"},
         ]},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "committed"
    rem = {res["op_no"]: res["remaining"] for res in r.json()["results"]}
    assert rem["OP-A"] == {"S1": 6.0, "S1a": 4.0}
    assert rem["DSP-B1"] == {"S1": 4.0, "S1a": 3.0}

    # 批量 dry-run:同批内两个 dispose 依次消耗,第二个超可用量 -> 整体无效
    r = client.post("/api/v1/batch/validate", json={"operations": [
        {"type": "dispose", "op_no": "DSP-V1",
         "items": [{"sample_id": "S1", "amount": 3, "reason": "污染"}]},
        {"type": "dispose", "op_no": "DSP-V2",
         "items": [{"sample_id": "S1", "amount": 2, "reason": "污染"}]},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["conflicts"][0]["batch_index"] == 1
    assert data["conflicts"][0]["code"] == "insufficient_available"
    # dry-run 不写主库
    assert client.get("/api/v1/operations/DSP-V1").status_code == 404
    bal = client.get("/api/v1/samples/S1/balance").json()
    assert bal["samples"][0]["stored_remaining"] == 4.0

    # 单操作 dry-run:合法
    r = client.post("/api/v1/validate", json={
        "type": "dispose", "op_no": "DSP-V3",
        "items": [{"sample_id": "S1", "amount": 4, "reason": "全部报废"}],
    })
    assert r.json()["valid"] is True
    assert r.json()["results"][0]["remaining"] == {"S1": 0.0}
    assert client.get("/api/v1/operations/DSP-V3").status_code == 404


def test_dispose_rolls_back_when_later_batch_op_conflicts(client):
    _sample(client, "OP-1", "S1", 10)
    # dispose 成功但后续 aliquot 余量不足 -> dispose 也回滚
    r = client.post("/api/v1/batch", json={"operations": [
        {"type": "dispose", "op_no": "DSP-R",
         "items": [{"sample_id": "S1", "amount": 2, "reason": "污染"}]},
        {"type": "aliquot", "op_no": "OP-R", "parent_id": "S1",
         "outputs": [{"sample_id": "S9", "amount": 99}]},
    ]})
    assert r.status_code == 409
    assert client.get("/api/v1/operations/DSP-R").status_code == 404
    bal = client.get("/api/v1/samples/S1/balance").json()
    assert bal["samples"][0]["stored_remaining"] == 10.0


# ---------------------------------------------------------------- 服务层直测

def test_service_dispose_versions_and_balance(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    init_db(conn)
    results = service.submit_batch(conn, [
        s_sampling := _SamplingOp("OP-1", "S1", 10.0),
    ])
    conn.close()

    # 经文件库重新打开,验证迁移后的库也能跑销毁(触发器/表齐备)
    conn = connect(db)
    from app.schemas import DisposeOp, DisposeItemIn
    results = service.submit_batch(conn, [DisposeOp(
        type="dispose", op_no="DSP-1",
        items=[DisposeItemIn(sample_id="S1", amount=6.0, reason="污染")],
    )])
    out = results[0]
    assert out.type == "dispose"
    assert out.disposed[0].from_version == 0
    assert out.disposed[0].to_version == 1
    b = service.get_balance(conn, "S1", 1e-6)
    assert b.consistent is True
    assert b.stored_remaining == 4.0
    assert b.total_out == 6.0
    conn.close()


# 服务层直测用的轻量取样构造
from app.schemas import SamplingOp  # noqa: E402


def _SamplingOp(op_no, sid, amount, unit="mL"):
    return SamplingOp(
        type="sampling", op_no=op_no, sample_id=sid,
        material="M", amount=amount, unit=unit,
    )
