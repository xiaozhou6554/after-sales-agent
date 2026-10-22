"""物流权威源的不可变只读快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TrackingEvent:
    time: datetime
    status: str
    description: str


@dataclass(frozen=True)
class LogisticsSnapshot:
    tenant_id: str
    order_id: str
    tracking_number: str
    status: str
    events: tuple[TrackingEvent, ...]
    exception_code: str | None
    estimated_delivery_at: datetime | None
    source_version: str
    observed_at: datetime
