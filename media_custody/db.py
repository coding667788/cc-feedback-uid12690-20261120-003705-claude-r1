"""SQLite 连接与模式定义。

设计要点：
- 全部状态都在单一 SQLite 库内，批次在 BEGIN IMMEDIATE ... COMMIT 中应用，
  要么整批落库要么整体回滚，重启后不会出现"旧快照 + 新状态"的混搭。
- events 表是唯一的全局事件序（seq 自增），verify 的独立小模型按此重放。
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    tx       TEXT UNIQUE NOT NULL,
    batch_id TEXT,
    type     TEXT NOT NULL,
    payload  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    person_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    grp       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    medium_id     TEXT PRIMARY KEY,
    custodian     TEXT NOT NULL,
    witness       TEXT NOT NULL,
    location      TEXT NOT NULL,
    box           TEXT,
    box_gen       INTEGER,
    current_proof TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boxes (
    box_id     TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    seal       TEXT NOT NULL,
    custodian  TEXT NOT NULL,
    witness    TEXT NOT NULL,
    location   TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('sealed', 'open'))
);

CREATE TABLE IF NOT EXISTS handovers (
    handover_id   TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    box           TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    media         TEXT NOT NULL,
    new_custodian TEXT NOT NULL,
    new_witness   TEXT NOT NULL,
    to_location   TEXT NOT NULL,
    out_confirmed INTEGER NOT NULL DEFAULT 0,
    in_confirmed  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'stale')),
    stale_reason  TEXT,
    created_tx    TEXT NOT NULL,
    resolved_tx   TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    handover_id TEXT NOT NULL,
    medium_id   TEXT NOT NULL,
    prior_proof TEXT NOT NULL,
    PRIMARY KEY (handover_id, medium_id)
);

CREATE TABLE IF NOT EXISTS proofs (
    proof_id     TEXT PRIMARY KEY,
    medium_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    tx           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    box          TEXT,
    generation   INTEGER,
    custodian    TEXT NOT NULL,
    witness      TEXT NOT NULL,
    location     TEXT NOT NULL,
    prior        TEXT,
    broken       INTEGER NOT NULL DEFAULT 0,
    break_reason TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    location     TEXT NOT NULL,
    seq          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id TEXT NOT NULL,
    box         TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    seal        TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, box)
);

CREATE TABLE IF NOT EXISTS diffs (
    snapshot_id TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('missing', 'extra', 'wrong_generation')),
    box         TEXT NOT NULL,
    expected    TEXT,
    observed    TEXT,
    PRIMARY KEY (snapshot_id, kind, box)
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    snapshot_id   TEXT NOT NULL,
    box           TEXT NOT NULL,
    diff_kind     TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    fix           TEXT NOT NULL,
    signers       TEXT NOT NULL,
    tx            TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）库并保证模式存在。WAL + 外键 + 长忙等以支持并发批次。"""
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    conn.close()
