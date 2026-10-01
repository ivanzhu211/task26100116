"""SQLite 持久化层：建库、建表与连接管理。"""

from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS installments (
    id               TEXT PRIMARY KEY,
    principal_due    INTEGER NOT NULL,
    due_at           INTEGER NOT NULL,
    penalty_assessed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    id             TEXT PRIMARY KEY,
    installment_id TEXT NOT NULL REFERENCES installments (id),
    occurred_at    INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    purpose        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_installment
    ON payments (installment_id, purpose, occurred_at);

CREATE TABLE IF NOT EXISTS entries (
    rowid_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id       INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    installment_id TEXT NOT NULL,
    account        TEXT NOT NULL,
    debit          INTEGER NOT NULL DEFAULT 0,
    credit         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_entries_installment
    ON entries (installment_id, account);

CREATE INDEX IF NOT EXISTS idx_entries_entry_id
    ON entries (entry_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保表结构存在。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('settlement_now', 0)"
    )
    conn.commit()
    return conn
