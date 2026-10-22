"""分析投影用例。数据提取与持久化由注入的投影端口完成。"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class AnalyticsProjectionPort(Protocol):
    @property
    def metric_definitions(self) -> dict[str, dict]: ...

    def project(self, tenant_id: str, projection_version: int, *, as_of: date) -> dict: ...

    def raw_snapshots(
        self, tenant_id: str, *, metric_id: str, cohort_start: str, cohort_end: str
    ) -> list[dict]: ...

    def query_metrics(self, auth, *, metric_id: str, cohort_start: str,
                      cohort_end: str, active_release: dict | None) -> dict: ...


class AnalyticsProjectionService:
    def __init__(self, projection: AnalyticsProjectionPort) -> None:
        self._projection = projection

    @property
    def metric_definitions(self) -> dict[str, dict]:
        return self._projection.metric_definitions

    def project(self, tenant_id: str, projection_version: int, *, as_of: date) -> dict:
        return self._projection.project(tenant_id, projection_version, as_of=as_of)

    def raw_snapshots(self, tenant_id: str, *, metric_id: str, cohort_start: str,
                      cohort_end: str) -> list[dict]:
        return self._projection.raw_snapshots(
            tenant_id, metric_id=metric_id, cohort_start=cohort_start,
            cohort_end=cohort_end,
        )

    def query_metrics(self, auth, *, metric_id: str, cohort_start: str,
                      cohort_end: str, active_release: dict | None) -> dict:
        return self._projection.query_metrics(
            auth, metric_id=metric_id, cohort_start=cohort_start,
            cohort_end=cohort_end, active_release=active_release,
        )
