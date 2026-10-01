"""SplitCart 分期罚费结算库：数据类型定义。

金额一律为整数分，时间一律为 UTC 整数秒。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 宽限期：到期后 72 小时（秒）
GRACE_PERIOD_SECONDS = 72 * 3600

# 每期固定罚费金额（分）
PENALTY_AMOUNT = 500

# 收款用途
PURPOSE_PRINCIPAL = "principal"
PURPOSE_PENALTY = "penalty"

# 复式分录科目
ACCOUNT_CASH = "cash"  # 现金（资产）
ACCOUNT_PRINCIPAL_RECEIVABLE = "principal_receivable"  # 本金应收（资产）
ACCOUNT_PENALTY_RECEIVABLE = "penalty_receivable"  # 罚费应收（资产）
ACCOUNT_PENALTY_INCOME = "penalty_income"  # 罚费收入（损益）
ACCOUNT_REFUNDABLE = "refundable"  # 应退客户款（负债）
ACCOUNT_MERCHANT_PAYABLE = "merchant_payable"  # 应付商家款（负债）

# 分录类型
ENTRY_REGISTER = "register"
ENTRY_PAYMENT = "payment"
ENTRY_PENALTY_ACCRUAL = "penalty_accrual"
ENTRY_PENALTY_REVERSAL = "penalty_reversal"
ENTRY_RECLASSIFICATION = "reclassification"


@dataclass(frozen=True)
class EntryLine:
    """一条分录行：同一科目一行内借贷只取其一。"""

    account: str
    debit: int = 0
    credit: int = 0


@dataclass(frozen=True)
class Entry:
    """一条复式分录，借贷金额恒等。"""

    id: int
    installment_id: str
    kind: str
    memo: str
    lines: tuple[EntryLine, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InstallmentBalance:
    """期次余额视图。应收/收入/应退字段均可由分录独立汇总得到。"""

    installment_id: str
    principal_due: int  # 应还本金
    principal_received: int  # 本金实收
    principal_outstanding: int  # 本金应收余额
    penalty_active: bool  # 当前是否存在有效罚费
    penalty_received: int  # 罚费实收（现金，不因冲正减少）
    penalty_receivable: int  # 罚费应收余额（非负）
    refundable: int  # 应退客户款
    net_penalty_income: int  # 净罚费收入，只可能为 0 或 500
