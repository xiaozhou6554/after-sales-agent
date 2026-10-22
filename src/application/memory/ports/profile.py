"""长期记忆仓储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.memory.aggregates.memory_profile import MemoryProfile
from domain.memory.entities.memory_fact import MemoryFact


class ProfileRepositoryPort(Protocol):
    def load(self, tenant_id: str, user_id: str) -> MemoryProfile: ...

    def upsert(self, tenant_id: str, user_id: str, fact: MemoryFact) -> None: ...

    def cleanup_candidates(self, tenant_id: str, user_id: str) -> list[MemoryFact]: ...

    def delete_cleanup_candidates(
        self, now: datetime, limit: int, tenant_id: str | None = None
    ) -> int: ...


class ProfileUnavailableError(RuntimeError):
    """长期记忆主存储不可用；不得伪造空画像。"""
