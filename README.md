# task26100116

## SplitCart 分期罚费结算库

由上层直接调用的 Python 库，数据保存在 SQLite 中。金额一律为整数分，
时间一律为 UTC 整数秒。

### 四类操作

```python
from splitcart import SplitCart, Purpose

with SplitCart("splitcart.db") as sc:
    # 1. 登记期次：唯一标识、应还本金（分）、到期时刻（UTC 秒）
    sc.register_installment("inst-1", principal_due=10000, due_at=1_700_000_000)

    # 2. 推进结算时刻（只能前进）
    sc.advance_settlement(1_700_100_000)

    # 3. 记录收款：唯一标识、实际发生时刻、金额、用途（本金/罚费）
    sc.record_payment("pay-1", "inst-1", 1_700_050_000, 10000, Purpose.PRINCIPAL)

    # 4. 读取期次余额 / 读取全部分录
    balance = sc.read_balance("inst-1")
    entries = sc.list_entries("inst-1")
```

### 业务规则要点

- 宽限截止时刻 = 到期时刻 + 72 小时。结算时刻严格晚于截止时刻、且截止
  时刻及之前实际发生的本金收款合计不足应还本金时，计提 500 分罚费，
  每期最多一笔；截止时刻恰好收清视为及时还款。
- 判断依据始终是收款实际发生时刻。迟到补录证明截止前已收清本金时，
  通过新增冲正分录撤销罚费；已收取的罚费转为应退客户款，现金实收不变。
- 登记、收款、罚费计提及冲正均形成借贷相等的复式分录；已提交分录
  不可改删。本金应收、罚费应收、净罚费收入、应退款均可从分录独立汇总。
- 相同标识和内容的收款重复提交为幂等空操作；重复推进同一结算时刻
  不会重复计提。

### 验证

```bash
python3 -m pytest tests -q      # 自动化验收测试
python3 perf/benchmark.py       # 性能验证（10 万期次 / 30 万收款）
```
