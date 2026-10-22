"""订单/支付查询用例的输入输出数据。

PaymentQueryResult.outcome 语义（技术详细规格 26.6）：
- found：读到支付执行快照；
- unknown：订单归属成立但支付源无该订单支付记录（支付未知，不是错误）。
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.order.snapshots import OrderSnapshot, PaymentSnapshot

PAYMENT_OUTCOME_FOUND = "found"
PAYMENT_OUTCOME_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PaymentQueryResult:
    outcome: str
    snapshot: PaymentSnapshot | None
    order: OrderSnapshot | None = None

    @property
    def found(self) -> bool:
        return self.outcome == PAYMENT_OUTCOME_FOUND


def payment_result(
    snapshot: PaymentSnapshot | None,
    order: OrderSnapshot,
) -> PaymentQueryResult:
    if snapshot is None:
        return PaymentQueryResult(outcome=PAYMENT_OUTCOME_UNKNOWN, snapshot=None, order=order)
    return PaymentQueryResult(outcome=PAYMENT_OUTCOME_FOUND, snapshot=snapshot, order=order)


__all__ = [
    "OrderSnapshot",
    "PAYMENT_OUTCOME_FOUND",
    "PAYMENT_OUTCOME_UNKNOWN",
    "PaymentQueryResult",
    "payment_result",
]
