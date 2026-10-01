# task26100116

# SplitCart 分期罚费结算库

由上层直接调用的 Python 库，数据保存在 SQLite 中。金额为整数分，时间为 UTC 整数秒。

## 四类操作

```python
from splitcart import Ledger, PURPOSE_PRINCIPAL, PURPOSE_PENALTY

with Ledger("splitcart.db") as ledger:
    ledger.register_installment("inst-1", 10000, 1_700_000_000)   # 登记期次
    ledger.record_payment("pay-1", "inst-1", 1_700_000_100, 5000, PURPOSE_PRINCIPAL)  # 记录收款
    ledger.advance_time(1_700_000_000 + 72 * 3600 + 1)            # 推进结算时刻（只能前进）
    balance = ledger.get_balance("inst-1")                        # 读取期次余额
    entries = ledger.get_entries("inst-1")                        # 读取全部分录
```

## 规则要点

- 宽限截止 = 到期 + 72 小时；结算时刻严格晚于截止且截止前本金实收不足，计提 500 分罚费，每期最多一笔。
- 罚费是否有效只看收款的实际发生时刻，与补录顺序无关；迟到补录证明截止前收清则冲正罚费。
- 冲正不抹现金：已收罚费转为应退客户款，只确认应退、不执行退款。
- 登记、收款、计提、冲正均为借贷相等的复式分录；已提交分录不可改删，纠正通过新增分录表达。
- 相同标识和内容的收款重复提交为幂等空操作；重复推进到同一结算时刻不重复计提。

## 测试与性能验证

```bash
python3 -m unittest discover -s tests -v   # 验收测试
python3 perf/benchmark.py                  # 10 万期次 / 30 万收款下的耗时验证（上限 10 秒）
```
