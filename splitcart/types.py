"""SplitCart 分期罚费结算库的公共类型定义。

金额一律为整数分，时间一律为 UTC 整数秒。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: 到期后的宽限时长：72 小时（秒）
GRACE_SECONDS = 72 * 3600

#: 每期唯一一笔罚费的金额（分）
PENALTY_AMOUNT = 500


class Purpose(str, Enum):
    """收款用途：本金或罚费。"""

    PRINCIPAL = "principal"
    PENALTY = "penalty"


class Account(str, Enum):
    """复式记账科目。"""

    PRINCIPAL_RECEIVABLE = "principal_receivable"
    PENALTY_RECEIVABLE = "penalty_receivable"
    PENALTY_REVENUE = "penalty_revenue"
    REFUND_PAYABLE = "refund_payable"
    CASH = "cash"
    MERCHANT_SETTLEMENT = "merchant_settlement"


@dataclass(frozen=True)
class Balance:
    """单个期次在某个结算时刻下的余额视图。"""

    installment_id: str
    principal_due: int
    principal_received: int
    principal_outstanding: int
    penalty_effective: int
    penalty_received: int
    penalty_receivable: int
    refund_payable: int
    net_penalty_income: int


@dataclass(frozen=True)
class EntryLine:
    """一条已提交分录的单侧行（同一 entry_id 的多行借贷平衡）。"""

    entry_id: int
    kind: str
    installment_id: str
    account: Account
    debit: int
    credit: int
