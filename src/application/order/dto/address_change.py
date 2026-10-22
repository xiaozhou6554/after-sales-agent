"""收货地址修改的应用层数据：快照、待办单记录与提交结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domain.order.address import ShippingAddress


@dataclass(frozen=True)
class AddressOrderSnapshot:
    tenant_id: str
    order_id: str
    customer_id: str
    order_status: str
    payment_status: str
    has_active_refund: bool
    order_version: int


@dataclass(frozen=True)
class AddressChangeRecord:
    address_change_id: str
    tenant_id: str
    order_id: str
    customer_id: str
    actor_subject_id: str
    expected_order_version: int
    status: str
    address: ShippingAddress
    address_digest: str
    confirmation_id: str
    idempotency_key: str
    result_order_version: int | None = None
    error_code: str | None = None
    observed_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class AddressApplyResult:
    operation_id: str
    address_change_id: str
    status: str
    result_order_version: int | None
    error_code: str | None
    observed_at: datetime
    replayed: bool = False
