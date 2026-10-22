"""按长期事实衰减规则分批清理普通偏好。"""

from __future__ import annotations

from datetime import datetime, timezone

from application.memory.ports.profile import ProfileRepositoryPort


class MemoryMaintenanceUseCase:
    def __init__(self, repository: ProfileRepositoryPort, *, batch_size: int = 500) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须为正数")
        self._repository = repository
        self._batch_size = batch_size

    def run(self, now: datetime | None = None, *, tenant_id: str | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        deleted_total = 0
        while True:
            deleted = self._repository.delete_cleanup_candidates(
                moment, self._batch_size, tenant_id=tenant_id
            )
            deleted_total += deleted
            if deleted < self._batch_size:
                return deleted_total
