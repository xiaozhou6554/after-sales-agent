"""任务原子创建、租约与查询的应用端口。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from application.common.events import EventEnvelope
from application.common.tasks.models import TaskLease, TaskRecord, ToolInvocationLease


class TaskRepositoryPort(Protocol):
    def create(
        self,
        *,
        tenant_id: str,
        task_id: str,
        customer_id: str,
        actor_subject_id: str,
        thread_id: str,
        task_type: str,
        input_masked: dict,
        input_digest: str,
        checkpoint_ref: str | None,
        idempotency_key: str,
        deadline_at: datetime,
        event: EventEnvelope,
    ) -> TaskRecord: ...

    def get(self, tenant_id: str, task_id: str) -> TaskRecord | None: ...

    def get_by_idempotency(
        self, tenant_id: str, actor_subject_id: str, task_type: str, idempotency_key: str
    ) -> TaskRecord | None: ...

    def claim(
        self, tenant_id: str, task_type: str, worker_id: str, *, lease_s: int = 30
    ) -> TaskLease | None: ...

    def complete(
        self,
        lease: TaskLease,
        result_ref: str,
        effect: Callable[[Any, TaskRecord], None] | None = None,
    ) -> TaskRecord: ...

    def fail(self, lease: TaskLease, error: str) -> TaskRecord: ...


class ToolInvocationRepositoryPort(Protocol):
    def reserve(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        tool_name: str,
        normalized_args_digest: str,
        args_summary: dict,
        access_mode: str,
        worker_id: str,
        lease_s: int = 30,
    ) -> ToolInvocationLease | None: ...

    def finish(
        self,
        lease: ToolInvocationLease,
        *,
        status: str,
        result_summary: dict | None = None,
        error: str | None = None,
    ) -> None: ...
