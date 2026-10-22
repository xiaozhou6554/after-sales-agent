"""业务事务内写入集成事件的应用端口。"""

from __future__ import annotations

from typing import Any, Protocol

from application.common.events import EventEnvelope


class OutboxPort(Protocol):
    def enqueue(
        self,
        connection: Any,
        event: EventEnvelope,
        *,
        aggregate_type: str,
        aggregate_id: str,
        thread_id: str | None = None,
    ) -> None: ...
