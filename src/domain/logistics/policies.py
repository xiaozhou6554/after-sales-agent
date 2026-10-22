"""物流异常只依据轨迹源事实及显式时间，不推断配送承诺。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from domain.logistics.snapshots import LogisticsSnapshot

STALE_AFTER = timedelta(hours=72)
TERMINAL_STATUSES = frozenset({"delivered", "returned", "cancelled"})


@dataclass(frozen=True)
class DeliveryException:
    exception_code: str
    description: str
    detected_at: datetime
    evidence_refs: tuple[str, ...]


def delivery_exceptions(
    snapshot: LogisticsSnapshot, now: datetime
) -> tuple[DeliveryException, ...]:
    if snapshot.exception_code:
        detected_at = snapshot.events[-1].time if snapshot.events else snapshot.observed_at
        return (
            DeliveryException(
                snapshot.exception_code,
                "物流源记录了异常状态，请联系售后核实。",
                detected_at,
                (snapshot.tracking_number, snapshot.source_version),
            ),
        )
    if snapshot.status in TERMINAL_STATUSES or not snapshot.events:
        return ()
    latest = max(event.time for event in snapshot.events)
    if now - latest < STALE_AFTER:
        return ()
    return (
        DeliveryException(
            "TRACKING_STALE",
            "物流轨迹超过72小时未更新，建议联系物流或创建工单核实。",
            latest + STALE_AFTER,
            (snapshot.tracking_number, snapshot.source_version),
        ),
    )
