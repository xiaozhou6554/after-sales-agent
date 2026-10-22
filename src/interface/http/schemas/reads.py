"""只读工具结果 DTO 的传输形态（《技术详细规格》26.6）。

query_order/query_payment/query_logistics 等只读工具分别返回具名结果 DTO 及
snapshot_version、observed_at，不返回 ORM 对象；金额一律整数分加币种。
"""

from __future__ import annotations

from pydantic import Field

from interface.http.schemas.common import StrictModel


class TrackingPointModel(StrictModel):
    time: str = Field(min_length=1)  # UTC ISO-8601（带 Z）
    status: str = Field(min_length=1)
    description: str = Field(min_length=1)


class OrderSummaryModel(StrictModel):
    order_id: str = Field(min_length=1)
    owner_customer_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    payment_status: str = Field(min_length=1)
    fulfillment_status: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int = Field(ge=0)
    snapshot_version: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)


class PaymentSummaryModel(StrictModel):
    payment_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    paid_minor: int = Field(ge=0)
    refunded_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    snapshot_version: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)


class LogisticsSummaryModel(StrictModel):
    tracking_number: str = Field(min_length=1)
    status: str = Field(min_length=1)
    tracking: list[TrackingPointModel] = Field(default_factory=list)
    exception_code: str | None = None
    estimated_delivery_at: str | None = None
    snapshot_version: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
