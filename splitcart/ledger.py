"""SplitCart 分期罚费结算账本：上层直接调用的入口类。"""

from __future__ import annotations

from typing import List

from .models import (
    ACCOUNT_CASH,
    ACCOUNT_MERCHANT_PAYABLE,
    ACCOUNT_PENALTY_INCOME,
    ACCOUNT_PENALTY_RECEIVABLE,
    ACCOUNT_PRINCIPAL_RECEIVABLE,
    ACCOUNT_REFUNDABLE,
    ENTRY_PAYMENT,
    ENTRY_PENALTY_ACCRUAL,
    ENTRY_PENALTY_REVERSAL,
    ENTRY_RECLASSIFICATION,
    ENTRY_REGISTER,
    GRACE_PERIOD_SECONDS,
    PENALTY_AMOUNT,
    PURPOSE_PENALTY,
    PURPOSE_PRINCIPAL,
    Entry,
    EntryLine,
    InstallmentBalance,
)
from .store import Store


class Ledger:
    """分期罚费结算账本。

    提供四类操作：登记期次、记录收款、推进结算时刻、读取期次余额及分录。
    调用按顺序执行，不做并发控制。
    """

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def settlement_at(self) -> int:
        """当前结算时刻（UTC 秒）。"""
        return self._store.get_settlement_at()

    def register_installment(
        self, installment_id: str, principal_due: int, due_at: int
    ) -> None:
        """登记期次：唯一标识、应还本金（分）、到期时刻（UTC 秒）。"""
        if principal_due <= 0:
            raise ValueError("principal_due 必须为正整数分")
        existing = self._store.get_installment(installment_id)
        if existing is not None:
            _, old_due, old_due_at, *_ = existing
            if old_due == principal_due and old_due_at == due_at:
                return  # 相同内容重复登记：幂等空操作
            raise ValueError(f"期次 {installment_id!r} 已存在且内容不一致")
        try:
            self._store.insert_installment(installment_id, principal_due, due_at)
            self._post_entry(
                installment_id,
                ENTRY_REGISTER,
                f"登记期次，应还本金 {principal_due} 分",
                [
                    (ACCOUNT_PRINCIPAL_RECEIVABLE, principal_due, 0),
                    (ACCOUNT_MERCHANT_PAYABLE, 0, principal_due),
                ],
            )
            self._sync_penalty(installment_id)
            self._store.commit()
        except Exception:
            self._store.rollback()
            raise

    def record_payment(
        self,
        payment_id: str,
        installment_id: str,
        occurred_at: int,
        amount: int,
        purpose: str,
    ) -> None:
        """记录收款。相同标识和内容的重复提交为幂等空操作。"""
        if purpose not in (PURPOSE_PRINCIPAL, PURPOSE_PENALTY):
            raise ValueError(f"非法收款用途: {purpose!r}")
        if amount <= 0:
            raise ValueError("amount 必须为正整数分")
        existing = self._store.get_payment(payment_id)
        if existing is not None:
            _, inst_id, occurred, amt, purp = existing
            if (inst_id, occurred, amt, purp) == (installment_id, occurred_at, amount, purpose):
                return  # 相同标识和内容：幂等空操作
            raise ValueError(f"收款 {payment_id!r} 已存在且内容不一致")
        row = self._store.get_installment(installment_id)
        if row is None:
            raise KeyError(f"期次 {installment_id!r} 不存在")
        if occurred_at > self._store.get_settlement_at():
            raise ValueError("收款实际发生时刻不能晚于当前结算时刻")
        (
            _,
            principal_due,
            due_at,
            principal_received,
            _paid_by_deadline,
            _penalty_received,
            _active,
        ) = row
        if purpose == PURPOSE_PRINCIPAL and principal_received + amount > principal_due:
            raise ValueError("本金累计实收超过该期应还本金")
        try:
            self._store.insert_payment(payment_id, installment_id, occurred_at, amount, purpose)
            if purpose == PURPOSE_PRINCIPAL:
                by_deadline = occurred_at <= due_at + GRACE_PERIOD_SECONDS
                self._store.add_principal_payment(installment_id, amount, by_deadline)
                self._post_entry(
                    installment_id,
                    ENTRY_PAYMENT,
                    f"本金收款 {payment_id}",
                    [
                        (ACCOUNT_CASH, amount, 0),
                        (ACCOUNT_PRINCIPAL_RECEIVABLE, 0, amount),
                    ],
                )
            else:
                self._store.add_penalty_payment(installment_id, amount)
                self._post_penalty_payment_entry(installment_id, payment_id, amount)
            self._sync_penalty(installment_id)
            self._store.commit()
        except Exception:
            self._store.rollback()
            raise

    def advance_time(self, settlement_at: int) -> None:
        """推进结算时刻，只能前进；重复推进到同一时刻为空操作。"""
        current = self._store.get_settlement_at()
        if settlement_at < current:
            raise ValueError("结算时刻只能前进")
        if settlement_at == current:
            return
        try:
            self._store.set_settlement_at(settlement_at)
            for row in self._store.installments_pending_accrual(
                settlement_at, GRACE_PERIOD_SECONDS
            ):
                self._accrue_penalty(row[0])
            self._store.commit()
        except Exception:
            self._store.rollback()
            raise

    def get_balance(self, installment_id: str) -> InstallmentBalance:
        """读取期次余额。"""
        row = self._store.get_installment(installment_id)
        if row is None:
            raise KeyError(f"期次 {installment_id!r} 不存在")
        (
            _,
            principal_due,
            _due_at,
            principal_received,
            _paid_by_deadline,
            penalty_received,
            active,
        ) = row
        sums = self._store.account_sums(installment_id)

        def balance_of(account: str) -> int:
            debit, credit = sums.get(account, (0, 0))
            return debit - credit

        return InstallmentBalance(
            installment_id=installment_id,
            principal_due=principal_due,
            principal_received=principal_received,
            principal_outstanding=balance_of(ACCOUNT_PRINCIPAL_RECEIVABLE),
            penalty_active=bool(active),
            penalty_received=penalty_received,
            penalty_receivable=balance_of(ACCOUNT_PENALTY_RECEIVABLE),
            refundable=-balance_of(ACCOUNT_REFUNDABLE),
            net_penalty_income=-balance_of(ACCOUNT_PENALTY_INCOME),
        )

    def get_entries(self, installment_id: str) -> List[Entry]:
        """读取期次的全部已提交分录（按提交顺序）。"""
        return [
            Entry(
                id=entry_id,
                installment_id=installment_id,
                kind=kind,
                memo=memo,
                lines=tuple(
                    EntryLine(account=account, debit=debit, credit=credit)
                    for account, debit, credit in lines
                ),
            )
            for entry_id, kind, memo, lines in self._store.list_entries(installment_id)
        ]

    # ---- 内部：分录与罚费状态机 ----

    def _post_entry(self, installment_id, kind, memo, lines) -> None:
        total_debit = sum(debit for _, debit, _ in lines)
        total_credit = sum(credit for _, _, credit in lines)
        assert total_debit == total_credit, "分录借贷必须相等"
        self._store.insert_entry(installment_id, kind, memo, lines)

    def _post_penalty_payment_entry(self, installment_id, payment_id, amount) -> None:
        """罚费收款：先冲减罚费应收余额，超出部分列为应退客户款。"""
        debit, credit = self._store.account_sums(installment_id).get(
            ACCOUNT_PENALTY_RECEIVABLE, (0, 0)
        )
        receivable = debit - credit
        to_receivable = min(amount, max(receivable, 0))
        to_refundable = amount - to_receivable
        lines = [(ACCOUNT_CASH, amount, 0)]
        if to_receivable:
            lines.append((ACCOUNT_PENALTY_RECEIVABLE, 0, to_receivable))
        if to_refundable:
            lines.append((ACCOUNT_REFUNDABLE, 0, to_refundable))
        self._post_entry(installment_id, ENTRY_PAYMENT, f"罚费收款 {payment_id}", lines)

    def _sync_penalty(self, installment_id: str) -> None:
        """按当前结算时刻与已知收款，使期次罚费状态符合规则。"""
        row = self._store.get_installment(installment_id)
        _, principal_due, due_at, _, paid_by_deadline, _, active = row
        settlement_at = self._store.get_settlement_at()
        desired = (
            settlement_at > due_at + GRACE_PERIOD_SECONDS
            and paid_by_deadline < principal_due
        )
        if desired and not active:
            self._accrue_penalty(installment_id)
        elif active and not desired:
            self._reverse_penalty(installment_id)

    def _accrue_penalty(self, installment_id: str) -> None:
        """计提罚费：借罚费应收，贷罚费收入；并吸收此前多缴形成的应退款。"""
        self._post_entry(
            installment_id,
            ENTRY_PENALTY_ACCRUAL,
            "宽限截止未收清本金，计提罚费",
            [
                (ACCOUNT_PENALTY_RECEIVABLE, PENALTY_AMOUNT, 0),
                (ACCOUNT_PENALTY_INCOME, 0, PENALTY_AMOUNT),
            ],
        )
        debit, credit = self._store.account_sums(installment_id).get(
            ACCOUNT_REFUNDABLE, (0, 0)
        )
        refundable = credit - debit
        absorb = min(refundable, PENALTY_AMOUNT)
        if absorb > 0:
            self._post_entry(
                installment_id,
                ENTRY_RECLASSIFICATION,
                "罚费生效，应退客户款抵充罚费应收",
                [
                    (ACCOUNT_REFUNDABLE, absorb, 0),
                    (ACCOUNT_PENALTY_RECEIVABLE, 0, absorb),
                ],
            )
        self._store.set_penalty_active(installment_id, True)

    def _reverse_penalty(self, installment_id: str) -> None:
        """冲正罚费：迟到补录证明截止前已收清本金。

        现金实收不变：未收部分冲减罚费应收，已收部分转为应退客户款。
        """
        debit, credit = self._store.account_sums(installment_id).get(
            ACCOUNT_PENALTY_RECEIVABLE, (0, 0)
        )
        receivable = debit - credit
        to_refundable = PENALTY_AMOUNT - receivable
        lines = [(ACCOUNT_PENALTY_INCOME, PENALTY_AMOUNT, 0)]
        if receivable > 0:
            lines.append((ACCOUNT_PENALTY_RECEIVABLE, 0, receivable))
        if to_refundable > 0:
            lines.append((ACCOUNT_REFUNDABLE, 0, to_refundable))
        self._post_entry(
            installment_id, ENTRY_PENALTY_REVERSAL, "补录证明截止前已收清本金，冲正罚费", lines
        )
        self._store.set_penalty_active(installment_id, False)
