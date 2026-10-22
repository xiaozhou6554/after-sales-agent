"""从版本化 Mock 物流源读取轨迹；仅用于开发和测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from application.logistics.ports.logistics_query import LogisticsDependencyError
from domain.logistics.snapshots import LogisticsSnapshot, TrackingEvent


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MockLogisticsAdapter:
    def __init__(self, source: Path) -> None:
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            if raw["dataset_version"] != "mock-v2" or not raw["synthetic_only"]:
                raise ValueError("物流源版本不匹配")
            self._source_version = raw["dataset_version"]
            self._shipments = tuple(raw["shipments"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LogisticsDependencyError("Mock 物流源不可用") from exc

    @property
    def source_version(self) -> str:
        return self._source_version

    def _snapshot(self, row: dict) -> LogisticsSnapshot:
        return LogisticsSnapshot(
            tenant_id=row["tenant_id"],
            order_id=row["order_id"],
            tracking_number=row["tracking_number"],
            status=row["status"],
            events=tuple(
                TrackingEvent(_date(e["time"]), e["status"], e["description"])
                for e in row["events"]
            ),
            exception_code=row["exception_code"],
            estimated_delivery_at=(
                _date(row["estimated_delivery_at"]) if row["estimated_delivery_at"] else None
            ),
            source_version=row["source_version"],
            observed_at=datetime.now(timezone.utc),
        )

    def by_order(self, tenant_id: str, order_id: str) -> tuple[LogisticsSnapshot, ...]:
        return tuple(
            self._snapshot(row)
            for row in self._shipments
            if row["tenant_id"] == tenant_id and row["order_id"] == order_id
        )

    def by_tracking(self, tenant_id: str, tracking_number: str) -> LogisticsSnapshot | None:
        return next(
            (
                self._snapshot(row)
                for row in self._shipments
                if row["tenant_id"] == tenant_id and row["tracking_number"] == tracking_number
            ),
            None,
        )
