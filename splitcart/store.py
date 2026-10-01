"""SQLite 持久化层：负责建表与最基础的数据存取。

只依赖标准库 sqlite3，不启动任何服务进程。
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS installments (
    id TEXT PRIMARY KEY,
    principal_due INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    principal_received INTEGER NOT NULL DEFAULT 0,
    principal_paid_by_deadline INTEGER NOT NULL DEFAULT 0,
    penalty_received INTEGER NOT NULL DEFAULT 0,
    penalty_active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    installment_id TEXT NOT NULL REFERENCES installments (id),
    occurred_at INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('principal', 'penalty'))
);
CREATE INDEX IF NOT EXISTS idx_payments_installment ON payments (installment_id);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    installment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entries_installment ON entries (installment_id);

CREATE TABLE IF NOT EXISTS entry_lines (
    entry_id INTEGER NOT NULL REFERENCES entries (id),
    installment_id TEXT NOT NULL,
    account TEXT NOT NULL,
    debit INTEGER NOT NULL DEFAULT 0,
    credit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entry_lines_entry ON entry_lines (entry_id);
CREATE INDEX IF NOT EXISTS idx_entry_lines_installment ON entry_lines (installment_id);
"""

# meta 表键
_KEY_SETTLEMENT_AT = "settlement_at"

# 期次行的元组字段顺序
_INSTALLMENT_COLS = (
    "id",
    "principal_due",
    "due_at",
    "principal_received",
    "principal_paid_by_deadline",
    "penalty_received",
    "penalty_active",
)


class Store:
    """对 SQLite 文件的薄封装，所有写操作由 Ledger 在事务内调用。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def get_settlement_at(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_KEY_SETTLEMENT_AT,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def set_settlement_at(self, value: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (_KEY_SETTLEMENT_AT, int(value)),
        )

    # ---- 期次 ----

    def get_installment(self, installment_id: str) -> Optional[Tuple]:
        """按标识取期次行，不存在返回 None。列顺序见 _INSTALLMENT_COLS。"""
        return self._conn.execute(
            f"SELECT {', '.join(_INSTALLMENT_COLS)} FROM installments WHERE id = ?",
            (installment_id,),
        ).fetchone()

    def insert_installment(self, installment_id: str, principal_due: int, due_at: int) -> None:
        self._conn.execute(
            "INSERT INTO installments (id, principal_due, due_at) VALUES (?, ?, ?)",
            (installment_id, int(principal_due), int(due_at)),
        )

    def add_principal_payment(self, installment_id: str, amount: int, by_deadline: bool) -> None:
        """累加本金实收；若实际发生时刻不晚于宽限截止，同时累加截止前实收。"""
        if by_deadline:
            self._conn.execute(
                "UPDATE installments SET principal_received = principal_received + ?, "
                "principal_paid_by_deadline = principal_paid_by_deadline + ? WHERE id = ?",
                (int(amount), int(amount), installment_id),
            )
        else:
            self._conn.execute(
                "UPDATE installments SET principal_received = principal_received + ? WHERE id = ?",
                (int(amount), installment_id),
            )

    def add_penalty_payment(self, installment_id: str, amount: int) -> None:
        self._conn.execute(
            "UPDATE installments SET penalty_received = penalty_received + ? WHERE id = ?",
            (int(amount), installment_id),
        )

    def set_penalty_active(self, installment_id: str, active: bool) -> None:
        self._conn.execute(
            "UPDATE installments SET penalty_active = ? WHERE id = ?",
            (1 if active else 0, installment_id),
        )

    def installments_pending_accrual(self, settlement_at: int, grace_seconds: int) -> List[Tuple]:
        """结算时刻推进后，需要补提罚费的期次。

        条件：当前无有效罚费、宽限截止时刻严格早于新结算时刻、
        截止前本金实收不足应还本金。
        """
        return self._conn.execute(
            f"SELECT {', '.join(_INSTALLMENT_COLS)} FROM installments "
            "WHERE penalty_active = 0 AND due_at + ? < ? "
            "AND principal_paid_by_deadline < principal_due",
            (int(grace_seconds), int(settlement_at)),
        ).fetchall()

    # ---- 收款 ----

    def get_payment(self, payment_id: str) -> Optional[Tuple]:
        return self._conn.execute(
            "SELECT id, installment_id, occurred_at, amount, purpose FROM payments WHERE id = ?",
            (payment_id,),
        ).fetchone()

    def insert_payment(
        self, payment_id: str, installment_id: str, occurred_at: int, amount: int, purpose: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO payments (id, installment_id, occurred_at, amount, purpose) "
            "VALUES (?, ?, ?, ?, ?)",
            (payment_id, installment_id, int(occurred_at), int(amount), purpose),
        )

    # ---- 分录 ----

    def insert_entry(
        self, installment_id: str, kind: str, memo: str, lines: List[Tuple[str, int, int]]
    ) -> int:
        """写入一条分录及其分录行，lines 为 (科目, 借, 贷)，返回分录 id。"""
        cur = self._conn.execute(
            "INSERT INTO entries (installment_id, kind, memo) VALUES (?, ?, ?)",
            (installment_id, kind, memo),
        )
        entry_id = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT INTO entry_lines (entry_id, installment_id, account, debit, credit) "
            "VALUES (?, ?, ?, ?, ?)",
            [(entry_id, installment_id, account, int(debit), int(credit)) for account, debit, credit in lines],
        )
        return entry_id

    def list_entries(self, installment_id: str) -> List[Tuple]:
        """按期次取全部分录，返回 (id, kind, memo, [(account, debit, credit), ...])。"""
        entries = self._conn.execute(
            "SELECT id, kind, memo FROM entries WHERE installment_id = ? ORDER BY id",
            (installment_id,),
        ).fetchall()
        result: List[Tuple] = []
        for entry_id, kind, memo in entries:
            lines = self._conn.execute(
                "SELECT account, debit, credit FROM entry_lines WHERE entry_id = ? ORDER BY rowid",
                (entry_id,),
            ).fetchall()
            result.append((entry_id, kind, memo, lines))
        return result

    def account_sums(self, installment_id: str) -> Dict[str, Tuple[int, int]]:
        """按期次汇总各科目借贷总额：{科目: (借方合计, 贷方合计)}。"""
        rows = self._conn.execute(
            "SELECT account, SUM(debit), SUM(credit) FROM entry_lines "
            "WHERE installment_id = ? GROUP BY account",
            (installment_id,),
        ).fetchall()
        return {account: (int(debit), int(credit)) for account, debit, credit in rows}

    # ---- 事务 ----

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()
