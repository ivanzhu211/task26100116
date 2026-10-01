"""SplitCart 结算库主入口：登记期次、记录收款、推进结算、读取余额与分录。"""

from __future__ import annotations

from .db import connect
from .types import (
    GRACE_SECONDS,
    PENALTY_AMOUNT,
    Account,
    Balance,
    EntryLine,
    Purpose,
)


class SplitCart:
    """分期罚费结算库。调用按顺序执行，不要求并发。"""

    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SplitCart":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 内部 helpers ----

    def _settlement_now(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'settlement_now'"
        ).fetchone()
        return int(row[0])

    def _post_entry(
        self, kind: str, installment_id: str, lines: list[tuple[Account, int, int]]
    ) -> None:
        total_debit = sum(line[1] for line in lines)
        total_credit = sum(line[2] for line in lines)
        if total_debit != total_credit or total_debit <= 0:
            raise AssertionError("unbalanced or empty journal entry")
        entry_id = self._conn.execute(
            "SELECT COALESCE(MAX(entry_id), 0) + 1 FROM entries"
        ).fetchone()[0]
        self._conn.executemany(
            "INSERT INTO entries (entry_id, kind, installment_id, account, debit, credit)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (entry_id, kind, installment_id, account.value, debit, credit)
                for account, debit, credit in lines
            ],
        )

    def _account_debit_balance(self, installment_id: str, account: Account) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)"
            " FROM entries WHERE installment_id = ? AND account = ?",
            (installment_id, account.value),
        ).fetchone()
        return int(row[0])

    def _payment_sum(
        self, installment_id: str, purpose: Purpose, deadline: int | None = None
    ) -> int:
        sql = (
            "SELECT COALESCE(SUM(amount), 0) FROM payments"
            " WHERE installment_id = ? AND purpose = ?"
        )
        params: list[object] = [installment_id, purpose.value]
        if deadline is not None:
            sql += " AND occurred_at <= ?"
            params.append(deadline)
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0])

    def _reconcile(self, installment_id: str) -> None:
        row = self._conn.execute(
            "SELECT principal_due, due_at, penalty_assessed FROM installments WHERE id = ?",
            (installment_id,),
        ).fetchone()
        principal_due, due_at, _ = row
        now = self._settlement_now()
        deadline = due_at + GRACE_SECONDS
        paid_by_deadline = self._payment_sum(
            installment_id, Purpose.PRINCIPAL, deadline
        )
        effective = (
            PENALTY_AMOUNT if now > deadline and paid_by_deadline < principal_due else 0
        )
        received = self._payment_sum(installment_id, Purpose.PENALTY)

        receivable = self._account_debit_balance(
            installment_id, Account.PENALTY_RECEIVABLE
        )
        refund = -self._account_debit_balance(installment_id, Account.REFUND_PAYABLE)
        revenue = -self._account_debit_balance(installment_id, Account.PENALTY_REVENUE)

        d_receivable = max(0, effective - received) - receivable
        d_refund = max(0, received - effective) - refund
        d_revenue = effective - revenue

        if d_receivable or d_refund or d_revenue:
            lines: list[tuple[Account, int, int]] = []
            if d_receivable > 0:
                lines.append((Account.PENALTY_RECEIVABLE, d_receivable, 0))
            elif d_receivable < 0:
                lines.append((Account.PENALTY_RECEIVABLE, 0, -d_receivable))
            if d_revenue > 0:
                lines.append((Account.PENALTY_REVENUE, 0, d_revenue))
            elif d_revenue < 0:
                lines.append((Account.PENALTY_REVENUE, -d_revenue, 0))
            if d_refund > 0:
                lines.append((Account.REFUND_PAYABLE, 0, d_refund))
            elif d_refund < 0:
                lines.append((Account.REFUND_PAYABLE, -d_refund, 0))
            self._post_entry("penalty_adjust", installment_id, lines)

        self._conn.execute(
            "UPDATE installments SET penalty_assessed = ? WHERE id = ?",
            (1 if effective else 0, installment_id),
        )

    # ---- 公共 API ----

    def register_installment(
        self, installment_id: str, principal_due: int, due_at: int
    ) -> None:
        """登记期次：唯一标识、应还本金（分）、到期时刻（UTC 秒）。"""
        if principal_due <= 0:
            raise ValueError("principal_due must be positive")
        exists = self._conn.execute(
            "SELECT 1 FROM installments WHERE id = ?", (installment_id,)
        ).fetchone()
        if exists:
            raise ValueError(f"installment already registered: {installment_id}")
        self._conn.execute(
            "INSERT INTO installments (id, principal_due, due_at) VALUES (?, ?, ?)",
            (installment_id, principal_due, due_at),
        )
        self._post_entry(
            "register",
            installment_id,
            [
                (Account.PRINCIPAL_RECEIVABLE, principal_due, 0),
                (Account.MERCHANT_SETTLEMENT, 0, principal_due),
            ],
        )
        self._reconcile(installment_id)
        self._conn.commit()

    def record_payment(
        self,
        payment_id: str,
        installment_id: str,
        occurred_at: int,
        amount: int,
        purpose: Purpose,
    ) -> bool:
        """记录收款。返回 True 表示新收款；同标识同内容重复提交返回 False。"""
        purpose = Purpose(purpose)
        if amount <= 0:
            raise ValueError("amount must be positive")
        inst = self._conn.execute(
            "SELECT principal_due FROM installments WHERE id = ?", (installment_id,)
        ).fetchone()
        if inst is None:
            raise ValueError(f"unknown installment: {installment_id}")
        if occurred_at > self._settlement_now():
            raise ValueError("payment occurred_at is after current settlement time")

        existing = self._conn.execute(
            "SELECT installment_id, occurred_at, amount, purpose FROM payments WHERE id = ?",
            (payment_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == (installment_id, occurred_at, amount, purpose.value):
                return False
            raise ValueError(f"payment id reused with different content: {payment_id}")

        if purpose is Purpose.PRINCIPAL:
            paid = self._payment_sum(installment_id, Purpose.PRINCIPAL)
            if paid + amount > inst[0]:
                raise ValueError("principal payments exceed principal_due")

        self._conn.execute(
            "INSERT INTO payments (id, installment_id, occurred_at, amount, purpose)"
            " VALUES (?, ?, ?, ?, ?)",
            (payment_id, installment_id, occurred_at, amount, purpose.value),
        )
        credit_account = (
            Account.PRINCIPAL_RECEIVABLE
            if purpose is Purpose.PRINCIPAL
            else Account.PENALTY_RECEIVABLE
        )
        self._post_entry(
            "payment",
            installment_id,
            [(Account.CASH, amount, 0), (credit_account, 0, amount)],
        )
        self._reconcile(installment_id)
        self._conn.commit()
        return True

    def advance_settlement(self, now: int) -> None:
        """推进结算时刻（只能前进）。"""
        current = self._settlement_now()
        if now < current:
            raise ValueError("settlement time cannot move backwards")
        if now == current:
            return
        self._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'settlement_now'", (now,)
        )
        rows = self._conn.execute(
            "SELECT i.id FROM installments i"
            " WHERE i.penalty_assessed = 0 AND i.due_at + ? < ?"
            " AND (SELECT COALESCE(SUM(p.amount), 0) FROM payments p"
            "      WHERE p.installment_id = i.id AND p.purpose = ?"
            "        AND p.occurred_at <= i.due_at + ?) < i.principal_due",
            (GRACE_SECONDS, now, Purpose.PRINCIPAL.value, GRACE_SECONDS),
        ).fetchall()
        for (installment_id,) in rows:
            self._reconcile(installment_id)
        self._conn.commit()

    def read_balance(self, installment_id: str) -> Balance:
        """读取期次余额。"""
        inst = self._conn.execute(
            "SELECT principal_due, penalty_assessed FROM installments WHERE id = ?",
            (installment_id,),
        ).fetchone()
        if inst is None:
            raise ValueError(f"unknown installment: {installment_id}")
        principal_due, penalty_assessed = inst
        return Balance(
            installment_id=installment_id,
            principal_due=principal_due,
            principal_received=self._payment_sum(installment_id, Purpose.PRINCIPAL),
            principal_outstanding=self._account_debit_balance(
                installment_id, Account.PRINCIPAL_RECEIVABLE
            ),
            penalty_effective=PENALTY_AMOUNT if penalty_assessed else 0,
            penalty_received=self._payment_sum(installment_id, Purpose.PENALTY),
            penalty_receivable=self._account_debit_balance(
                installment_id, Account.PENALTY_RECEIVABLE
            ),
            refund_payable=-self._account_debit_balance(
                installment_id, Account.REFUND_PAYABLE
            ),
            net_penalty_income=-self._account_debit_balance(
                installment_id, Account.PENALTY_REVENUE
            ),
        )

    def list_entries(self, installment_id: str) -> list[EntryLine]:
        """读取期次的全部已提交分录行，按提交顺序排列。"""
        rows = self._conn.execute(
            "SELECT entry_id, kind, installment_id, account, debit, credit"
            " FROM entries WHERE installment_id = ? ORDER BY rowid_seq",
            (installment_id,),
        ).fetchall()
        return [
            EntryLine(
                entry_id=row[0],
                kind=row[1],
                installment_id=row[2],
                account=Account(row[3]),
                debit=row[4],
                credit=row[5],
            )
            for row in rows
        ]
