"""企业订单、支付的只读业务快照（《技术详细规格》26.6 第 1262 行）。

- 快照是外部业务数据在本进程内的不可变投影，不重建企业主数据；
- snapshot_version 标识数据来源版本，observed_at 为本次读取时间；
- 金额一律 minor 单位整数，币种为 ISO 4217 三字码。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OrderSnapshot:
    tenant_id: str
    order_id: str
    owner_customer_id: str
    status: str
    payment_status: str
    fulfillment_status: str
    currency: str
    amount_minor: int
    version: int
    snapshot_version: str
    observed_at: datetime


@dataclass(frozen=True)
class PaymentSnapshot:
    tenant_id: str
    order_id: str
    payment_status: str
    paid_minor: int
    refunded_minor: int
    currency: str
    payment_ids: tuple[str, ...]
    snapshot_version: str
    observed_at: datetime
