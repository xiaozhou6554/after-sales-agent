"""后台任务状态与租约值。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TaskRecord:
    tenant_id: str
    task_id: str
    customer_id: str
    actor_subject_id: str
    thread_id: str
    task_type: str
    input_masked: dict
    input_digest: str
    status: str
    revision: int
    checkpoint_ref: str | None
    result_ref: str | None
    error: str | None
    idempotency_key: str
    deadline_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TaskLease:
    task: TaskRecord
    worker_id: str
    fencing_token: int


class TaskConflictError(RuntimeError):
    """同幂等键请求内容不同。"""


class TaskLeaseLostError(RuntimeError):
    """执行者租约已过期或被新执行者接管。"""


@dataclass(frozen=True)
class ToolInvocationLease:
    invocation_id: str
    tenant_id: str
    request_id: str
    tool_name: str
    attempt_count: int
    retry_count: int
    worker_id: str
    fencing_token: int


class ToolInvocationConflictError(RuntimeError):
    """相同调用键对应不同执行身份或访问模式。"""
