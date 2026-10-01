"""SplitCart 性能验证：10 万期次、30 万条收款的库上，
连续 1000 次分散期次的单笔补录及对应余额读取，合计须不超过 10 秒。

运行：python3 perf/benchmark.py
初始数据装载时间不计入耗时。
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from splitcart import GRACE_PERIOD_SECONDS, PURPOSE_PRINCIPAL, Ledger  # noqa: E402

N_INSTALLMENTS = 100_000
N_PAYMENTS = 300_000  # 每期 3 笔
N_OPS = 1_000
TIME_LIMIT_SECONDS = 10.0

DUE_AT = 1_700_000_000
DEADLINE = DUE_AT + GRACE_PERIOD_SECONDS
SETTLEMENT_AT = DEADLINE + 1  # 结算时刻已过宽限截止
PRINCIPAL_DUE = 100_000
SEED_PAYMENT_AMOUNT = 100  # 每期 3 笔种子本金收款


def seed_database(db_path: str) -> None:
    """直接以 SQL 批量装载初始数据（装载耗时不计入验证）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    from splitcart.store import SCHEMA

    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('settlement_at', ?)", (SETTLEMENT_AT,)
    )

    entries_sql = "INSERT INTO entries (id, installment_id, kind, memo) VALUES (?, ?, ?, '')"
    lines_sql = (
        "INSERT INTO entry_lines (entry_id, installment_id, account, debit, credit) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    entry_id = 0
    chunk = 5000
    for base in range(0, N_INSTALLMENTS, chunk):
        installments, payments, entries, lines = [], [], [], []
        for i in range(base, min(base + chunk, N_INSTALLMENTS)):
            inst_id = f"inst-{i}"
            installments.append((inst_id, PRINCIPAL_DUE, DUE_AT, 3 * SEED_PAYMENT_AMOUNT,
                                 3 * SEED_PAYMENT_AMOUNT, 0, 1))
            entry_id += 1  # 登记分录
            entries.append((entry_id, inst_id, "register"))
            lines.append((entry_id, inst_id, "principal_receivable", PRINCIPAL_DUE, 0))
            lines.append((entry_id, inst_id, "merchant_payable", 0, PRINCIPAL_DUE))
            for j in range(3):  # 每期 3 笔本金收款及分录
                pid = f"seed-{i}-{j}"
                payments.append((pid, inst_id, DUE_AT + j, SEED_PAYMENT_AMOUNT, "principal"))
                entry_id += 1
                entries.append((entry_id, inst_id, "payment"))
                lines.append((entry_id, inst_id, "cash", SEED_PAYMENT_AMOUNT, 0))
                lines.append((entry_id, inst_id, "principal_receivable", 0, SEED_PAYMENT_AMOUNT))
            entry_id += 1  # 罚费计提分录（结算时刻已过截止且截止前未收清）
            entries.append((entry_id, inst_id, "penalty_accrual"))
            lines.append((entry_id, inst_id, "penalty_receivable", 500, 0))
            lines.append((entry_id, inst_id, "penalty_income", 0, 500))
        conn.executemany(
            "INSERT INTO installments (id, principal_due, due_at, principal_received, "
            "principal_paid_by_deadline, penalty_received, penalty_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            installments,
        )
        conn.executemany(
            "INSERT INTO payments (id, installment_id, occurred_at, amount, purpose) "
            "VALUES (?, ?, ?, ?, ?)",
            payments,
        )
        conn.executemany(entries_sql, entries)
        conn.executemany(lines_sql, lines)
        conn.commit()
    conn.close()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="splitcart-perf-")
    db_path = os.path.join(tmp, "benchmark.db")

    t0 = time.perf_counter()
    seed_database(db_path)
    seed_elapsed = time.perf_counter() - t0
    print(f"初始数据装载（不计入验证耗时）: {seed_elapsed:.2f}s "
          f"({N_INSTALLMENTS} 期次, {N_PAYMENTS} 收款)")

    rng = random.Random(42)
    targets = rng.sample(range(N_INSTALLMENTS), N_OPS)

    ledger = Ledger(db_path)
    try:
        start = time.perf_counter()
        for seq, i in enumerate(targets):
            inst_id = f"inst-{i}"
            # 分散期次的单笔迟到补录（实际发生在截止前）+ 对应余额读取
            ledger.record_payment(
                f"backfill-{seq}", inst_id, DEADLINE - 1 - seq, 50, PURPOSE_PRINCIPAL
            )
            balance = ledger.get_balance(inst_id)
            assert balance.principal_received == 3 * SEED_PAYMENT_AMOUNT + 50
            assert balance.net_penalty_income in (0, 500)
        elapsed = time.perf_counter() - start
    finally:
        ledger.close()

    print(f"验证耗时: {elapsed:.3f}s / 上限 {TIME_LIMIT_SECONDS:.0f}s "
          f"({N_OPS} 次补录+余额读取, {N_OPS / elapsed:.0f} ops/s)")
    if elapsed > TIME_LIMIT_SECONDS:
        print("结果: 未达标")
        sys.exit(1)
    print("结果: 达标")


if __name__ == "__main__":
    main()
