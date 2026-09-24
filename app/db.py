"""SQLite 连接管理与建表脚本。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Generator

from .config import DB_PATH

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 样品主数据:余量以出入库流水为准并冗余保存 remaining;
-- version 为乐观锁版本,余量每被改动一次加 1,盘点据此判断"期间是否被其他操作改变"
CREATE TABLE IF NOT EXISTS samples (
    sample_id        TEXT PRIMARY KEY,
    material         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    initial_amount   REAL NOT NULL CHECK (initial_amount >= 0),
    remaining        REAL NOT NULL CHECK (remaining >= 0),
    version          INTEGER NOT NULL DEFAULT 0,
    location         TEXT,
    is_root          INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 操作记录:op_no 全局唯一,是幂等键;payload 规范化后入库用于比对重放
CREATE TABLE IF NOT EXISTS operations (
    op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    op_no      TEXT NOT NULL UNIQUE,
    op_type    TEXT NOT NULL CHECK (op_type IN ('sampling', 'aliquot', 'merge', 'dispose')),
    payload    TEXT NOT NULL,
    effect     TEXT NOT NULL,
    seq_index  INTEGER NOT NULL,
    batch_id   TEXT,
    location   TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 出入库流水(余量账本),与谱系边一一对应;sampling 的 in 流水无父边;
-- dispose 的 out 流水无谱系边而带 dispose_id,指向不可变销毁审计记录
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id    TEXT NOT NULL,
    op_id        INTEGER NOT NULL,
    edge_id      INTEGER,
    dispose_id   INTEGER,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount       REAL NOT NULL CHECK (amount >= 0),
    unit         TEXT NOT NULL,
    ref_no       TEXT NOT NULL,
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id),
    FOREIGN KEY (op_id)   REFERENCES operations(op_id),
    FOREIGN KEY (edge_id) REFERENCES lineage_edges(edge_id),
    FOREIGN KEY (dispose_id) REFERENCES dispose_records(dispose_id)
);

-- 谱系边:parent -> child,parent 为 NULL 表示取样(源头)
CREATE TABLE IF NOT EXISTS lineage_edges (
    edge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id      TEXT,
    child_id       TEXT NOT NULL,
    op_id          INTEGER NOT NULL,
    amount         REAL NOT NULL CHECK (amount >= 0),
    unit           TEXT NOT NULL,
    ref_no         TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES samples(sample_id),
    FOREIGN KEY (child_id)  REFERENCES samples(sample_id),
    FOREIGN KEY (op_id)     REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_child  ON lineage_edges(child_id);
CREATE INDEX IF NOT EXISTS idx_ledger_sample ON ledger_entries(sample_id);

-- 预留单:一个预留号为多个样品分别锁定数量;active=锁定中,released=已整单释放
CREATE TABLE IF NOT EXISTS reservations (
    reservation_no TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    payload        TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at    TEXT
);

-- 预留明细:同一预留单内每个样品至多一条(UNIQUE 兜底)
CREATE TABLE IF NOT EXISTS reservation_items (
    item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_no TEXT NOT NULL,
    sample_id      TEXT NOT NULL,
    amount         REAL NOT NULL CHECK (amount > 0),
    unit           TEXT NOT NULL,
    seq_index      INTEGER NOT NULL,
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no),
    FOREIGN KEY (sample_id)      REFERENCES samples(sample_id),
    UNIQUE (reservation_no, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_ritems_reservation ON reservation_items(reservation_no);
CREATE INDEX IF NOT EXISTS idx_ritems_sample      ON reservation_items(sample_id);

-- 预留审计事件:预留 / 幂等重放 / 释放 / 查询的结果快照均持久化
CREATE TABLE IF NOT EXISTS reservation_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_no TEXT,
    action         TEXT NOT NULL CHECK (action IN ('reserve', 'replay', 'release', 'query')),
    ref_id         TEXT,
    detail         TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no)
);

CREATE INDEX IF NOT EXISTS idx_revents_reservation ON reservation_events(reservation_no);

-- 核销领用单:一个核销号(consumption_no,幂等键)对一张未释放预留单做一次分次领用;
-- 头表不可变(触发器禁止 UPDATE/DELETE),result_snapshot 保存首次提交的原始结果
CREATE TABLE IF NOT EXISTS consumptions (
    consumption_no  TEXT PRIMARY KEY,
    reservation_no  TEXT NOT NULL,
    payload         TEXT NOT NULL,
    result_snapshot TEXT NOT NULL,
    operator        TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no)
);

-- 核销明细:逐项记录从预留单中实际领用的样品量,随核销单在同一事务写入;
-- 触发器禁止 UPDATE/DELETE(不可变审计)。核销成功即扣减 samples.remaining 并 +1 版本;
-- 核销量计入余量账本重算(非谱系出账),并把该预留明细的剩余锁定量冲减为「未核销部分」
CREATE TABLE IF NOT EXISTS consumption_items (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    consumption_no  TEXT NOT NULL,
    reservation_no  TEXT NOT NULL,
    sample_id       TEXT NOT NULL,
    amount          REAL NOT NULL CHECK (amount > 0),
    unit            TEXT NOT NULL,
    seq_index       INTEGER NOT NULL,
    from_version    INTEGER NOT NULL,
    to_version      INTEGER NOT NULL,
    remaining_after REAL NOT NULL CHECK (remaining_after >= 0),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (consumption_no) REFERENCES consumptions(consumption_no),
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no),
    FOREIGN KEY (sample_id)      REFERENCES samples(sample_id),
    UNIQUE (consumption_no, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_citems_consumption ON consumption_items(consumption_no);
CREATE INDEX IF NOT EXISTS idx_citems_reservation ON consumption_items(reservation_no);
CREATE INDEX IF NOT EXISTS idx_citems_sample      ON consumption_items(sample_id);

CREATE TRIGGER IF NOT EXISTS trg_consumption_no_update
BEFORE UPDATE ON consumptions
BEGIN
    SELECT RAISE(ABORT, 'consumptions 不可变:禁止更新核销领用单');
END;

CREATE TRIGGER IF NOT EXISTS trg_consumption_no_delete
BEFORE DELETE ON consumptions
BEGIN
    SELECT RAISE(ABORT, 'consumptions 不可变:禁止删除核销领用单');
END;

CREATE TRIGGER IF NOT EXISTS trg_citem_no_update
BEFORE UPDATE ON consumption_items
BEGIN
    SELECT RAISE(ABORT, 'consumption_items 不可变:禁止更新核销明细');
END;

CREATE TRIGGER IF NOT EXISTS trg_citem_no_delete
BEFORE DELETE ON consumption_items
BEGIN
    SELECT RAISE(ABORT, 'consumption_items 不可变:禁止删除核销明细');
END;

-- 已执行的数据库迁移登记(幂等升级用)
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 盘点批次:open=已建批(仅有快照),submitted=已提交实盘结果(含差异),
-- confirmed=已确认并落调整/改库存
CREATE TABLE IF NOT EXISTS stocktakes (
    stocktake_no  TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'submitted', 'confirmed')),
    payload       TEXT NOT NULL,
    submit_payload TEXT,
    note          TEXT,
    operator      TEXT,
    reason        TEXT,
    submitted_at  TEXT,
    confirmed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 盘点明细:建批时写入系统余量/单位/版本快照;提交实盘时写 counted/diff/submitted_version
CREATE TABLE IF NOT EXISTS stocktake_items (
    item_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_no      TEXT NOT NULL,
    sample_id         TEXT NOT NULL,
    seq_index         INTEGER NOT NULL,
    snapshot_remaining REAL NOT NULL,
    snapshot_unit     TEXT NOT NULL,
    snapshot_version  INTEGER NOT NULL,
    counted_amount    REAL CHECK (counted_amount IS NULL OR counted_amount >= 0),
    difference        REAL,
    submitted_version INTEGER,
    FOREIGN KEY (stocktake_no) REFERENCES stocktakes(stocktake_no),
    FOREIGN KEY (sample_id)     REFERENCES samples(sample_id),
    UNIQUE (stocktake_no, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_sitems_stocktake ON stocktake_items(stocktake_no);
CREATE INDEX IF NOT EXISTS idx_sitems_sample     ON stocktake_items(sample_id);

-- 盘点调整流水:确认时逐项写入,是余量账本的一部分;
-- adjustment 方向的金额为实盘量-系统量(可正可负),非谱系转移故无 operation 关联
CREATE TABLE IF NOT EXISTS stocktake_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_no  TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    system_amount REAL NOT NULL,
    counted_amount REAL NOT NULL CHECK (counted_amount >= 0),
    delta         REAL NOT NULL,
    unit          TEXT NOT NULL,
    operator      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    from_version  INTEGER NOT NULL,
    to_version    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (stocktake_no) REFERENCES stocktakes(stocktake_no),
    FOREIGN KEY (sample_id)    REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_sadj_stocktake ON stocktake_adjustments(stocktake_no);
CREATE INDEX IF NOT EXISTS idx_sadj_sample    ON stocktake_adjustments(sample_id);

-- 销毁/报废审计记录:逐项明细,随单事务写入,触发器禁止 UPDATE/DELETE(不可变);
-- 销毁量同时出库(ledger_entries.direction='out'),不产生谱系边
CREATE TABLE IF NOT EXISTS dispose_records (
    dispose_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id        INTEGER NOT NULL,
    op_no        TEXT NOT NULL,
    sample_id    TEXT NOT NULL,
    amount       REAL NOT NULL CHECK (amount > 0),
    unit         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    operator     TEXT,
    seq_index    INTEGER NOT NULL,
    from_version INTEGER NOT NULL,
    to_version   INTEGER NOT NULL,
    remaining_after REAL NOT NULL CHECK (remaining_after >= 0),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (op_id) REFERENCES operations(op_id),
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_dispose_op     ON dispose_records(op_no);
CREATE INDEX IF NOT EXISTS idx_dispose_sample ON dispose_records(sample_id);

CREATE TRIGGER IF NOT EXISTS trg_dispose_no_update
BEFORE UPDATE ON dispose_records
BEGIN
    SELECT RAISE(ABORT, 'dispose_records 不可变:禁止更新销毁审计记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_dispose_no_delete
BEFORE DELETE ON dispose_records
BEGIN
    SELECT RAISE(ABORT, 'dispose_records 不可变:禁止删除销毁审计记录');
END;

-- 位置交接单:一次提交多个样品的原位置 -> 目标位置;
-- transfer_no 为幂等键,单事务内逐项校验样品仍处于请求所述原位置
CREATE TABLE IF NOT EXISTS location_transfers (
    transfer_no TEXT PRIMARY KEY,
    operator    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 位置交接明细:逐项记录变更前后位置,随交接单单事务写入,触发器禁止 UPDATE/DELETE
CREATE TABLE IF NOT EXISTS location_transfer_items (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_no   TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    seq_index     INTEGER NOT NULL,
    from_location TEXT,
    to_location   TEXT NOT NULL,
    operator      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (transfer_no) REFERENCES location_transfers(transfer_no),
    FOREIGN KEY (sample_id)   REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_ltransfer_no     ON location_transfer_items(transfer_no);
CREATE INDEX IF NOT EXISTS idx_ltransfer_sample ON location_transfer_items(sample_id);

CREATE TRIGGER IF NOT EXISTS trg_ltransfer_hdr_no_update
BEFORE UPDATE ON location_transfers
BEGIN
    SELECT RAISE(ABORT, 'location_transfers 不可变:禁止更新位置交接单');
END;

CREATE TRIGGER IF NOT EXISTS trg_ltransfer_hdr_no_delete
BEFORE DELETE ON location_transfers
BEGIN
    SELECT RAISE(ABORT, 'location_transfers 不可变:禁止删除位置交接单');
END;

CREATE TRIGGER IF NOT EXISTS trg_ltransfer_no_update
BEFORE UPDATE ON location_transfer_items
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_items 不可变:禁止更新位置交接明细');
END;

CREATE TRIGGER IF NOT EXISTS trg_ltransfer_no_delete
BEFORE DELETE ON location_transfer_items
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_items 不可变:禁止删除位置交接明细');
END;

-- 位置交接整单冲正单:一个冲正号(reversal_no,幂等键)对一张已完成的原位置交接单
-- (transfer_no)整单冲正;头表不可变(触发器禁止 UPDATE/DELETE)
CREATE TABLE IF NOT EXISTS location_transfer_reversals (
    reversal_no TEXT PRIMARY KEY,
    transfer_no TEXT NOT NULL,
    operator    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (transfer_no) REFERENCES location_transfers(transfer_no)
);

-- 冲正明细:逐项记录冲正前位置(= 原单目标位置)与恢复后的位置(= 原单原位置,含 null),
-- 与冲正单头同一事务写入;触发器禁止 UPDATE/DELETE(不可变审计),原交接明细保留不动
CREATE TABLE IF NOT EXISTS location_transfer_reversal_items (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reversal_no   TEXT NOT NULL,
    transfer_no   TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    seq_index     INTEGER NOT NULL,
    from_location TEXT NOT NULL,
    to_location   TEXT,
    operator      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (reversal_no) REFERENCES location_transfer_reversals(reversal_no),
    FOREIGN KEY (transfer_no) REFERENCES location_transfers(transfer_no),
    FOREIGN KEY (sample_id)   REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_lrev_no     ON location_transfer_reversal_items(reversal_no);
CREATE INDEX IF NOT EXISTS idx_lrev_transfer ON location_transfer_reversal_items(transfer_no);
CREATE INDEX IF NOT EXISTS idx_lrev_sample ON location_transfer_reversal_items(sample_id);

CREATE TRIGGER IF NOT EXISTS trg_lrev_hdr_no_update
BEFORE UPDATE ON location_transfer_reversals
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_reversals 不可变:禁止更新位置交接冲正单');
END;

CREATE TRIGGER IF NOT EXISTS trg_lrev_hdr_no_delete
BEFORE DELETE ON location_transfer_reversals
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_reversals 不可变:禁止删除位置交接冲正单');
END;

CREATE TRIGGER IF NOT EXISTS trg_lrev_no_update
BEFORE UPDATE ON location_transfer_reversal_items
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_reversal_items 不可变:禁止更新位置交接冲正明细');
END;

CREATE TRIGGER IF NOT EXISTS trg_lrev_no_delete
BEFORE DELETE ON location_transfer_reversal_items
BEGIN
    SELECT RAISE(ABORT, 'location_transfer_reversal_items 不可变:禁止删除位置交接冲正明细');
END;
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开连接并开启外键 / WAL / 立刻拿到写锁的基础配置。"""
    path = db_path or DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表并执行增量迁移(对旧版本数据库补齐新列/新表)。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)


# 增量迁移:每项在 schema_migrations 中登记一次,重复启动不重复执行
_MIGRATIONS = (
    # 旧库 samples 没有 version 列时补齐(乐观锁),存量样品版本从 0 起
    (
        "samples_add_version",
        "ALTER TABLE samples ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
        "samples",
        "version",
    ),
    # 盘点头补提交请求体列(早于正式发布前的中间版本库兼容)
    (
        "stocktakes_add_submit_payload",
        "ALTER TABLE stocktakes ADD COLUMN submit_payload TEXT",
        "stocktakes",
        "submit_payload",
    ),
    # 出库流水补销毁审计外键(销毁/报废操作的 out 流水无谱系边,以此关联审计明细)
    (
        "ledger_entries_add_dispose_id",
        "ALTER TABLE ledger_entries ADD COLUMN dispose_id INTEGER",
        "ledger_entries",
        "dispose_id",
    ),
)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    for name, ddl, table, column in _MIGRATIONS:
        done = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ).fetchone()
        if done is not None:
            continue
        # 登记与 DDL 同事务;ALTER TABLE 在 SQLite 中自动提交但无嵌套事务,
        # 此处连接为 isolation_level=None(autocommit),逐条执行即可
        if not _column_exists(conn, table, column):
            conn.execute(ddl)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
        )
    _migrate_operations_add_dispose(conn)


def _migrate_operations_add_dispose(conn: sqlite3.Connection) -> None:
    """旧库 operations 的 op_type CHECK 不含 dispose,SQLite 无法改 CHECK,
    需按 12 步表重建流程换表(外键关闭/恢复,数据原样拷贝)。"""
    name = "operations_add_dispose_type"
    done = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
    ).fetchone()
    if done is not None:
        return
    # 新库(_SCHEMA 已含 dispose 的 CHECK)无需重建
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
    ).fetchone()["sql"]
    if "'dispose'" in sql:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
        )
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            "CREATE TABLE operations_new ("
            " op_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " op_no TEXT NOT NULL UNIQUE,"
            " op_type TEXT NOT NULL CHECK"
            " (op_type IN ('sampling', 'aliquot', 'merge', 'dispose')),"
            " payload TEXT NOT NULL, effect TEXT NOT NULL, seq_index INTEGER NOT NULL,"
            " batch_id TEXT, location TEXT,"
            " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        )
        conn.execute(
            "INSERT INTO operations_new (op_id, op_no, op_type, payload, effect,"
            " seq_index, batch_id, location, created_at)"
            " SELECT op_id, op_no, op_type, payload, effect, seq_index, batch_id,"
            " location, created_at FROM operations"
        )
        conn.execute("DROP TABLE operations")
        conn.execute("ALTER TABLE operations_new RENAME TO operations")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def get_conn(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI 依赖(生成器):每个请求一个连接,结束自动关闭并提交隐式事务。"""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
