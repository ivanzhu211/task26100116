"""SplitCart 性能验证。

在 10 万期次、30 万条收款的库中，连续 1000 次分散期次的单笔补录
及对应余额读取，合计不得超过 10 秒。初始数据装载不计时。
固定随机种子，结果可复现。运行：python3 perf/benchmark.py [db_path]
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from splitcart import GRACE_SECONDS, Purpose, SplitCart

N_INSTALLMENTS = 100_000
N_PAYMENTS = 300_000
N_OPS = 1_000
TIME_BUDGET_SECONDS = 10.0
BASE = 1_700_000_000
SEED = 20261001


def bulk_load(db_path: str) -> int:
    """直接以 SQL 装载初始数据（行内容等价于登记+收款），返回结算时刻。"""
    rng = random.Random(SEED)
    sc = SplitCart(db_path)
    conn = sc._conn
    now = BASE + 200_000 + GRACE_SECONDS + 1

    installments = []
    entries = []
    entry_id = 0
    for index in range(N_INSTALLMENTS):
        iid = f"i{index}"
        due_at = BASE + (index % 100_000)
        principal = rng.choice([50_000, 100_000, 200_000])
        installments.append((iid, principal, due_at))
        entry_id += 1
        entries.append((entry_id, "register", iid, "principal_receivable", principal, 0))
        entries.append((entry_id, "register", iid, "merchant_settlement", 0, principal))

    principal_of = {iid: p for iid, p, _ in installments}
    occurred_by_iid: dict[str, list[int]] = {}
    for _ in range(N_PAYMENTS):
        iid = f"i{rng.randrange(N_INSTALLMENTS)}"
        occurred_by_iid.setdefault(iid, []).append(rng.randint(0, now - 1))

    payment_rows = []
    seq = 0
    for iid, occurred_list in occurred_by_iid.items():
        principal = principal_of[iid]
        total = rng.randint(0, principal)
        weights = [rng.random() + 0.01 for _ in occurred_list]
        weight_sum = sum(weights)
        amounts = [int(total * w / weight_sum) for w in weights]
        amounts[-1] += total - sum(amounts)
        for occurred, amount in zip(occurred_list, amounts):
            if amount <= 0:
                continue
            seq += 1
            payment_rows.append((f"p{seq}", iid, occurred, amount))
            entry_id += 1
            entries.append((entry_id, "payment", iid, "cash", amount, 0))
            entries.append((entry_id, "payment", iid, "principal_receivable", 0, amount))

    conn.executemany(
        "INSERT INTO installments (id, principal_due, due_at) VALUES (?, ?, ?)",
        installments,
    )
    conn.executemany(
        "INSERT INTO payments (id, installment_id, occurred_at, amount, purpose)"
        " VALUES (?, ?, ?, ?, 'principal')",
        payment_rows,
    )
    conn.executemany(
        "INSERT INTO entries (entry_id, kind, installment_id, account, debit, credit)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        entries,
    )
    conn.commit()

    # 通过正常 API 推进结算时刻，完成罚费计提（装载阶段，不计时）
    sc.advance_settlement(now)
    sc.close()
    return now


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/splitcart_perf.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)

    print(f"装载 {N_INSTALLMENTS} 期次 / {N_PAYMENTS} 收款（不计时）...")
    load_start = time.perf_counter()
    now = bulk_load(db_path)
    print(f"装载完成，耗时 {time.perf_counter() - load_start:.1f}s（不计入考核）")

    rng = random.Random(SEED + 1)
    sc = SplitCart(db_path)
    targets = [f"i{rng.randrange(N_INSTALLMENTS)}" for _ in range(N_OPS)]

    start = time.perf_counter()
    for seq, iid in enumerate(targets):
        balance = sc.read_balance(iid)
        if balance.principal_outstanding > 0:
            amount = rng.randint(1, balance.principal_outstanding)
            occurred = rng.randint(0, now)
            sc.record_payment(f"bench{seq}", iid, occurred, amount, Purpose.PRINCIPAL)
        sc.read_balance(iid)
    elapsed = time.perf_counter() - start
    sc.close()

    print(f"{N_OPS} 次补录+余额读取合计耗时: {elapsed:.3f}s（预算 {TIME_BUDGET_SECONDS}s）")
    if elapsed > TIME_BUDGET_SECONDS:
        print("FAIL: 超过时间预算")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
