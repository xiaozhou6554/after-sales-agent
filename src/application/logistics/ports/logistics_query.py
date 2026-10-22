"""按租户查询物流事实的端口。"""

from __future__ import annotations

from typing import Protocol

from domain.logistics.snapshots import LogisticsSnapshot


class LogisticsDependencyError(RuntimeError):
    """物流源暂不可用。"""


class LogisticsQueryPort(Protocol):
    @property
    def source_version(self) -> str: ...

    def by_order(self, tenant_id: str, order_id: str) -> tuple[LogisticsSnapshot, ...]: ...

    def by_tracking(self, tenant_id: str, tracking_number: str) -> LogisticsSnapshot | None: ...
