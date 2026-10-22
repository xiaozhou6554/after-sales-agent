"""会话摘要存储端口。"""

from __future__ import annotations

from typing import Protocol

from application.memory.dto.session import SessionSnapshot


class SessionSummaryPort(Protocol):
    def load_snapshot(
        self, tenant_id: str, thread_id: str, *, subject_id: str
    ) -> SessionSnapshot: ...

    def save_summary(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        subject_id: str,
        expected_covered_until: int,
        covered_until: int,
        summary: str,
    ) -> None: ...
