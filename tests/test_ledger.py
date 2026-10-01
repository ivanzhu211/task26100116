"""SplitCart 分期罚费结算库验收测试。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

from splitcart import (
    GRACE_PERIOD_SECONDS,
    PENALTY_AMOUNT,
    PURPOSE_PENALTY,
    PURPOSE_PRINCIPAL,
    Ledger,
)

DUE_AT = 1_700_000_000  # 任意到期时刻（UTC 秒）
DEADLINE = DUE_AT + GRACE_PERIOD_SECONDS  # 宽限截止时刻
AFTER_DEADLINE = DEADLINE + 1


def recompute_expected(installments, payments, settlement_at):
    """独立重算：按全部已知收款与结算时刻推导每期余额。

    installments: {id: principal_due}（到期时刻统一为 DUE_AT）
    payments: [(installment_id, occurred_at, amount, purpose)]
    返回 {id: dict}，与 Ledger.get_balance 的关键字段对应。
    """
    expected = {}
    for inst_id, principal_due in installments.items():
        principal_received = 0
        paid_by_deadline = 0
        penalty_received = 0
        for pid, occurred_at, amount, purpose in payments:
            if pid != inst_id:
                continue
            if purpose == PURPOSE_PRINCIPAL:
                principal_received += amount
                if occurred_at <= DUE_AT + GRACE_PERIOD_SECONDS:
                    paid_by_deadline += amount
            else:
                penalty_received += amount
        effective = (
            PENALTY_AMOUNT
            if settlement_at > DUE_AT + GRACE_PERIOD_SECONDS
            and paid_by_deadline < principal_due
            else 0
        )
        expected[inst_id] = {
            "principal_received": principal_received,
            "principal_outstanding": principal_due - principal_received,
            "penalty_active": effective > 0,
            "penalty_received": penalty_received,
            "penalty_receivable": max(effective - penalty_received, 0),
            "refundable": max(penalty_received - effective, 0),
            "net_penalty_income": effective,
        }
    return expected


def assert_balances_match(testcase, ledger, expected):
    for inst_id, exp in expected.items():
        bal = ledger.get_balance(inst_id)
        for field, value in exp.items():
            testcase.assertEqual(
                getattr(bal, field), value, f"{inst_id}.{field} 与独立重算不一致"
            )
        testcase.assertIn(bal.net_penalty_income, (0, PENALTY_AMOUNT))


def assert_all_entries_balanced(testcase, ledger, installment_ids):
    for inst_id in installment_ids:
        for entry in ledger.get_entries(inst_id):
            debit = sum(line.debit for line in entry.lines)
            credit = sum(line.credit for line in entry.lines)
            testcase.assertEqual(debit, credit, f"分录 {entry.id} 借贷不平衡")


class DeadlineBoundaryTest(unittest.TestCase):
    """验收一：截止时刻收清 vs 晚 1 秒收清。"""

    def test_payoff_exactly_at_deadline_has_no_penalty(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("on-time", 10000, DUE_AT)
            ledger.advance_time(AFTER_DEADLINE)
            # 迟到补录：实际恰好在宽限截止时刻收清本金
            ledger.record_payment("p1", "on-time", DEADLINE, 10000, PURPOSE_PRINCIPAL)
            bal = ledger.get_balance("on-time")
            self.assertFalse(bal.penalty_active)
            self.assertEqual(bal.penalty_receivable, 0)
            self.assertEqual(bal.net_penalty_income, 0)
            self.assertEqual(bal.principal_outstanding, 0)

    def test_payoff_one_second_late_still_owes_500(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("late", 10000, DUE_AT)
            ledger.advance_time(AFTER_DEADLINE)
            # 实际在截止时刻后 1 秒才收清本金，罚费不撤销
            ledger.record_payment("p1", "late", DEADLINE + 1, 10000, PURPOSE_PRINCIPAL)
            bal = ledger.get_balance("late")
            self.assertTrue(bal.penalty_active)
            self.assertEqual(bal.penalty_receivable, PENALTY_AMOUNT)
            self.assertEqual(bal.net_penalty_income, PENALTY_AMOUNT)
            self.assertEqual(bal.principal_outstanding, 0)


class CollectedPenaltyReversalTest(unittest.TestCase):
    """验收二：罚费已收后补录及时本金收款。"""

    def test_reversal_keeps_cash_and_creates_refundable(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("inst", 10000, DUE_AT)
            ledger.advance_time(AFTER_DEADLINE)
            # 罚费已计提并全额收讫
            ledger.record_payment("pen1", "inst", AFTER_DEADLINE, PENALTY_AMOUNT, PURPOSE_PENALTY)
            mid = ledger.get_balance("inst")
            self.assertEqual(mid.penalty_received, PENALTY_AMOUNT)
            self.assertEqual(mid.penalty_receivable, 0)
            self.assertEqual(mid.net_penalty_income, PENALTY_AMOUNT)
            # 迟到补录：实际在截止时刻前已收清本金
            ledger.record_payment("pri1", "inst", DEADLINE - 3600, 10000, PURPOSE_PRINCIPAL)
            bal = ledger.get_balance("inst")
            self.assertEqual(bal.net_penalty_income, 0)  # 罚费收入归零
            self.assertEqual(bal.penalty_received, PENALTY_AMOUNT)  # 现金实收不变
            self.assertEqual(bal.penalty_receivable, 0)
            self.assertEqual(bal.refundable, PENALTY_AMOUNT)  # 形成 500 分应退款
            # 冲正通过新增分录表达，既有分录保持不变
            kinds = [e.kind for e in ledger.get_entries("inst")]
            self.assertEqual(kinds, ["register", "penalty_accrual", "payment", "payment", "penalty_reversal"])


class OutOfOrderDuplicateReopenTest(unittest.TestCase):
    """验收三：乱序补录、重复提交、重开数据库。"""

    def _run_scenario(self, ledger):
        ledger.register_installment("a", 10000, DUE_AT)
        ledger.register_installment("b", 20000, DUE_AT)
        ledger.register_installment("c", 5000, DUE_AT)
        ledger.advance_time(AFTER_DEADLINE)  # 三期均无收款，全部计提罚费
        # 乱序补录：先录晚发生的，再录早发生的
        ledger.record_payment("a-pen", "a", AFTER_DEADLINE, PENALTY_AMOUNT, PURPOSE_PENALTY)
        ledger.record_payment("a-p1", "a", DEADLINE - 10, 6000, PURPOSE_PRINCIPAL)
        ledger.record_payment("a-p3", "a", DEADLINE - 5, 4000, PURPOSE_PRINCIPAL)  # a 实际及时收清
        ledger.record_payment("b-p1", "b", DEADLINE - 1, 8000, PURPOSE_PRINCIPAL)  # b 部分还款，不免罚
        ledger.record_payment("b-pen", "b", AFTER_DEADLINE, 200, PURPOSE_PENALTY)  # b 罚费部分实收
        ledger.record_payment("c-p1", "c", DEADLINE + 1, 5000, PURPOSE_PRINCIPAL)  # c 晚 1 秒收清
        # 重复提交：相同标识和内容，不得增加实收或分录
        ledger.record_payment("a-p1", "a", DEADLINE - 10, 6000, PURPOSE_PRINCIPAL)
        ledger.record_payment("a-pen", "a", AFTER_DEADLINE, PENALTY_AMOUNT, PURPOSE_PENALTY)
        ledger.advance_time(AFTER_DEADLINE)  # 重复推进到同一时刻
        ledger.advance_time(AFTER_DEADLINE + 86400)
        ledger.advance_time(AFTER_DEADLINE + 86400)  # 再次重复推进

    _INSTALLMENTS = {"a": 10000, "b": 20000, "c": 5000}
    _PAYMENTS = [
        ("a", AFTER_DEADLINE, PENALTY_AMOUNT, PURPOSE_PENALTY),
        ("a", DEADLINE - 10, 6000, PURPOSE_PRINCIPAL),
        ("a", DEADLINE - 5, 4000, PURPOSE_PRINCIPAL),
        ("b", DEADLINE - 1, 8000, PURPOSE_PRINCIPAL),
        ("b", AFTER_DEADLINE, 200, PURPOSE_PENALTY),
        ("c", DEADLINE + 1, 5000, PURPOSE_PRINCIPAL),
    ]

    def test_balances_match_independent_recompute_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "splitcart.db")
            ledger = Ledger(db_path)
            self._run_scenario(ledger)
            entries_before = {i: ledger.get_entries(i) for i in self._INSTALLMENTS}
            ledger.close()

            # 正常关闭后重新打开：余额、分录、幂等性必须保持
            ledger = Ledger(db_path)
            try:
                expected = recompute_expected(
                    self._INSTALLMENTS, self._PAYMENTS, AFTER_DEADLINE + 86400
                )
                assert_balances_match(self, ledger, expected)
                assert_all_entries_balanced(self, ledger, self._INSTALLMENTS)
                # 已提交分录在重开后保持不变
                for inst_id, entries in entries_before.items():
                    self.assertEqual(ledger.get_entries(inst_id), entries)
                # 重开后重复提交仍是幂等空操作，分录数量不变
                ledger.record_payment("a-p1", "a", DEADLINE - 10, 6000, PURPOSE_PRINCIPAL)
                for inst_id, entries in entries_before.items():
                    self.assertEqual(ledger.get_entries(inst_id), entries)
                # 业务断言：a 罚费冲正且应退 500；b 部分还款不免罚；c 晚 1 秒仍欠 500
                bal_a = ledger.get_balance("a")
                self.assertEqual((bal_a.net_penalty_income, bal_a.refundable), (0, PENALTY_AMOUNT))
                bal_b = ledger.get_balance("b")
                self.assertEqual(
                    (bal_b.net_penalty_income, bal_b.penalty_receivable),
                    (PENALTY_AMOUNT, PENALTY_AMOUNT - 200),
                )
                bal_c = ledger.get_balance("c")
                self.assertEqual(bal_c.penalty_receivable, PENALTY_AMOUNT)
            finally:
                ledger.close()


class IdempotencyAndValidationTest(unittest.TestCase):
    def test_duplicate_payment_adds_no_entry_or_cash(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(DUE_AT)
            ledger.record_payment("p", "i", DUE_AT, 3000, PURPOSE_PRINCIPAL)
            count = len(ledger.get_entries("i"))
            ledger.record_payment("p", "i", DUE_AT, 3000, PURPOSE_PRINCIPAL)
            self.assertEqual(len(ledger.get_entries("i")), count)
            self.assertEqual(ledger.get_balance("i").principal_received, 3000)

    def test_conflicting_payment_id_raises(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(DUE_AT)
            ledger.record_payment("p", "i", DUE_AT, 3000, PURPOSE_PRINCIPAL)
            with self.assertRaises(ValueError):
                ledger.record_payment("p", "i", DUE_AT, 3001, PURPOSE_PRINCIPAL)

    def test_settlement_time_cannot_go_backwards(self):
        with Ledger(":memory:") as ledger:
            ledger.advance_time(100)
            with self.assertRaises(ValueError):
                ledger.advance_time(99)
            ledger.advance_time(100)  # 同一时刻：空操作

    def test_payment_after_settlement_time_rejected(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(DUE_AT)
            with self.assertRaises(ValueError):
                ledger.record_payment("p", "i", DUE_AT + 1, 100, PURPOSE_PRINCIPAL)

    def test_principal_overpayment_rejected(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(DUE_AT)
            with self.assertRaises(ValueError):
                ledger.record_payment("p", "i", DUE_AT, 10001, PURPOSE_PRINCIPAL)

    def test_partial_payment_before_deadline_does_not_reduce_penalty(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(AFTER_DEADLINE)
            ledger.record_payment("p", "i", DEADLINE - 1, 9999, PURPOSE_PRINCIPAL)
            bal = ledger.get_balance("i")
            self.assertEqual(bal.penalty_receivable, PENALTY_AMOUNT)
            self.assertEqual(bal.net_penalty_income, PENALTY_AMOUNT)

    def test_entry_derived_balances_match_business_balances(self):
        with Ledger(":memory:") as ledger:
            ledger.register_installment("i", 10000, DUE_AT)
            ledger.advance_time(AFTER_DEADLINE)
            ledger.record_payment("pen", "i", AFTER_DEADLINE, 300, PURPOSE_PENALTY)
            ledger.record_payment("pri", "i", DEADLINE - 1, 10000, PURPOSE_PRINCIPAL)
            sums = {}
            for entry in ledger.get_entries("i"):
                for line in entry.lines:
                    debit, credit = sums.get(line.account, (0, 0))
                    sums[line.account] = (debit + line.debit, credit + line.credit)
            bal = ledger.get_balance("i")
            self.assertEqual(bal.penalty_receivable, 0)
            self.assertEqual(bal.refundable, 300)
            self.assertEqual(bal.net_penalty_income, 0)
            self.assertEqual(sums["refundable"], (0, 300))
            self.assertEqual(sums["penalty_income"], (PENALTY_AMOUNT, PENALTY_AMOUNT))


if __name__ == "__main__":
    unittest.main()
