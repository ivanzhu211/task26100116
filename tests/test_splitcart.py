"""SplitCart 结算库验收测试。"""

from __future__ import annotations

import random

import pytest

from splitcart import GRACE_SECONDS, PENALTY_AMOUNT, Purpose, SplitCart

DUE = 1_000_000
DEADLINE = DUE + GRACE_SECONDS


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "splitcart.db")


def expected_balance(installments, payments, now, installment_id):
    """独立重算器：仅根据全部已知收款与结算时刻推导余额。"""
    principal_due, due_at = installments[installment_id]
    deadline = due_at + GRACE_SECONDS
    principal = [p for p in payments if p[1] == installment_id and p[4] == "principal"]
    penalty = [p for p in payments if p[1] == installment_id and p[4] == "penalty"]
    paid_by_deadline = sum(p[3] for p in principal if p[2] <= deadline)
    effective = (
        PENALTY_AMOUNT if now > deadline and paid_by_deadline < principal_due else 0
    )
    penalty_received = sum(p[3] for p in penalty)
    return {
        "principal_received": sum(p[3] for p in principal),
        "principal_outstanding": principal_due - sum(p[3] for p in principal),
        "penalty_effective": effective,
        "penalty_received": penalty_received,
        "penalty_receivable": max(0, effective - penalty_received),
        "refund_payable": max(0, penalty_received - effective),
        "net_penalty_income": effective,
    }


def assert_entries_balanced(sc, installment_id):
    groups = {}
    for line in sc.list_entries(installment_id):
        debit, credit = groups.get(line.entry_id, (0, 0))
        groups[line.entry_id] = (debit + line.debit, credit + line.credit)
    assert groups, "expected at least one entry"
    for entry_id, (debit, credit) in groups.items():
        assert debit == credit, f"entry {entry_id} unbalanced"


def test_paid_exactly_at_deadline_has_no_penalty(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DEADLINE)
        assert sc.record_payment("p1", "i1", DEADLINE, 10000, Purpose.PRINCIPAL)
        sc.advance_settlement(DEADLINE + 1)
        balance = sc.read_balance("i1")
        assert balance.principal_outstanding == 0
        assert balance.penalty_effective == 0
        assert balance.penalty_receivable == 0
        assert balance.net_penalty_income == 0
        assert balance.refund_payable == 0
        assert_entries_balanced(sc, "i1")


def test_paid_one_second_after_deadline_still_owes_500(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DEADLINE + 1)
        sc.record_payment("p1", "i1", DEADLINE + 1, 10000, Purpose.PRINCIPAL)
        balance = sc.read_balance("i1")
        assert balance.principal_outstanding == 0
        assert balance.penalty_effective == PENALTY_AMOUNT
        assert balance.penalty_receivable == PENALTY_AMOUNT
        assert balance.net_penalty_income == PENALTY_AMOUNT
        assert balance.refund_payable == 0
        assert_entries_balanced(sc, "i1")


def test_collected_penalty_reversed_by_late_backfill(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DEADLINE + 10)
        assert sc.read_balance("i1").penalty_receivable == PENALTY_AMOUNT
        sc.record_payment("pen1", "i1", DEADLINE + 5, PENALTY_AMOUNT, Purpose.PENALTY)
        paid = sc.read_balance("i1")
        assert paid.penalty_received == PENALTY_AMOUNT
        assert paid.penalty_receivable == 0
        assert paid.net_penalty_income == PENALTY_AMOUNT

        # 迟到补录：证明截止时刻前本金已收清，必须撤销罚费
        sc.record_payment("pri1", "i1", DEADLINE - 1, 10000, Purpose.PRINCIPAL)
        balance = sc.read_balance("i1")
        assert balance.penalty_effective == 0
        assert balance.net_penalty_income == 0
        assert balance.penalty_received == PENALTY_AMOUNT  # 现金实收不变
        assert balance.penalty_receivable == 0
        assert balance.refund_payable == PENALTY_AMOUNT  # 形成应退客户款
        assert_entries_balanced(sc, "i1")


def test_partial_payment_before_deadline_does_not_reduce_penalty(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DEADLINE)
        sc.record_payment("p1", "i1", DEADLINE - 10, 9999, Purpose.PRINCIPAL)
        sc.advance_settlement(DEADLINE + 1)
        balance = sc.read_balance("i1")
        assert balance.penalty_effective == PENALTY_AMOUNT
        assert balance.penalty_receivable == PENALTY_AMOUNT
        assert balance.principal_outstanding == 1


def test_duplicate_payment_is_idempotent(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DUE)
        assert sc.record_payment("p1", "i1", DUE, 3000, Purpose.PRINCIPAL) is True
        entries_before = sc.list_entries("i1")
        balance_before = sc.read_balance("i1")
        assert sc.record_payment("p1", "i1", DUE, 3000, Purpose.PRINCIPAL) is False
        assert sc.list_entries("i1") == entries_before
        assert sc.read_balance("i1") == balance_before
        with pytest.raises(ValueError):
            sc.record_payment("p1", "i1", DUE, 3001, Purpose.PRINCIPAL)


def test_repeated_advance_is_idempotent(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        sc.advance_settlement(DEADLINE + 1)
        entries_before = sc.list_entries("i1")
        sc.advance_settlement(DEADLINE + 1)
        sc.advance_settlement(DEADLINE + 1)
        assert sc.list_entries("i1") == entries_before
        assert sc.read_balance("i1").net_penalty_income == PENALTY_AMOUNT
        with pytest.raises(ValueError):
            sc.advance_settlement(DEADLINE)


def test_validation_errors(db_path):
    with SplitCart(db_path) as sc:
        sc.register_installment("i1", 10000, DUE)
        with pytest.raises(ValueError):
            sc.register_installment("i1", 10000, DUE)
        with pytest.raises(ValueError):
            sc.record_payment("x", "i1", 5, 100, Purpose.PRINCIPAL)  # 超出结算时刻
        sc.advance_settlement(DUE)
        with pytest.raises(ValueError):
            sc.record_payment("x", "unknown", 1, 100, Purpose.PRINCIPAL)
        with pytest.raises(ValueError):
            sc.record_payment("x", "i1", 1, 10001, Purpose.PRINCIPAL)  # 超出本金
        with pytest.raises(ValueError):
            sc.record_payment("x", "i1", 1, 0, Purpose.PRINCIPAL)
        with pytest.raises(ValueError):
            sc.read_balance("unknown")


def test_out_of_order_backfill_and_reopen(db_path):
    """乱序补录 + 重复提交 + 重开：余额须与独立重算一致，分录平衡且不变。"""
    rng = random.Random(20261001)
    installments = {}
    payments = []
    used_payment_ids = set()
    now = 0

    with SplitCart(db_path) as sc:
        for index in range(30):
            iid = f"i{index}"
            due = DUE + rng.randint(0, 10_000)
            principal = rng.choice([5000, 10000, 20000])
            installments[iid] = (principal, due)
            sc.register_installment(iid, principal, due)

        for step in range(300):
            action = rng.random()
            if action < 0.25 or now == 0:
                now += rng.randint(1, 5000)
                sc.advance_settlement(now)
                continue
            iid = rng.choice(list(installments))
            principal, due = installments[iid]
            paid = sum(
                p[3] for p in payments if p[1] == iid and p[4] == "principal"
            )
            purpose = rng.choice([Purpose.PRINCIPAL, Purpose.PENALTY])
            occurred = rng.randint(0, now)
            if purpose is Purpose.PRINCIPAL:
                remaining = principal - paid
                if remaining == 0:
                    continue
                amount = rng.randint(1, remaining)
            else:
                amount = PENALTY_AMOUNT
            pid = f"pay{step}"
            if rng.random() < 0.15 and payments:
                # 重复提交一笔已有收款
                dup = rng.choice(payments)
                assert sc.record_payment(*dup[:1], *dup[1:2], *dup[2:3], *dup[3:4],
                                         Purpose(dup[4])) is False
                continue
            assert pid not in used_payment_ids
            used_payment_ids.add(pid)
            payment = (pid, iid, occurred, amount, purpose.value)
            assert sc.record_payment(pid, iid, occurred, amount, purpose) is True
            payments.append(payment)

        snapshot_entries = {
            iid: sc.list_entries(iid) for iid in installments
        }
        snapshot_balances = {iid: sc.read_balance(iid) for iid in installments}

    # 重开后：余额、分录、幂等性保持
    with SplitCart(db_path) as sc:
        for iid in installments:
            balance = sc.read_balance(iid)
            assert balance == snapshot_balances[iid]
            expected = expected_balance(installments, payments, now, iid)
            for field, value in expected.items():
                assert getattr(balance, field) == value, (iid, field)
            assert balance.net_penalty_income in (0, PENALTY_AMOUNT)
            assert sc.list_entries(iid) == snapshot_entries[iid]
            assert_entries_balanced(sc, iid)
        # 重开后重复提交仍幂等
        for payment in payments[:20]:
            assert sc.record_payment(
                payment[0], payment[1], payment[2], payment[3], Purpose(payment[4])
            ) is False
        for iid in installments:
            assert sc.list_entries(iid) == snapshot_entries[iid]
            assert sc.read_balance(iid) == snapshot_balances[iid]
