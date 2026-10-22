"""创建可恢复任务并按可信身份查询状态。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    require_tenant,
)
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.events import parse_event
from application.common.tasks.models import TaskConflictError, TaskRecord
from application.common.tasks.ports import TaskRepositoryPort

BACKGROUND_TASK_DEADLINE_S = 3600


class TaskUseCases:
    def __init__(self, repository: TaskRepositoryPort) -> None:
        self._repository = repository

    def create(
        self,
        *,
        auth: AuthContext,
        trace: TraceContext,
        task_type: str,
        input_masked: dict,
        thread_id: str,
        idempotency_key: str,
        customer_id: str | None = None,
        checkpoint_ref: str | None = None,
        deadline_at: datetime | None = None,
    ) -> TaskRecord:
        if customer_id is None:
            allowed = auth.data_scope.customer_ids
            if len(allowed) != 1 or allowed[0] == "*":
                raise ValueError("代客任务必须指定目标客户")
            target = allowed[0]
        else:
            target = customer_id
        require_customer_access(auth, target)
        if not task_type or not thread_id or not 16 <= len(idempotency_key) <= 128:
            raise ValueError("任务类型、会话和幂等键不符合契约")
        if task_type == "agent.chat" and not checkpoint_ref:
            raise ValueError("对话任务必须有可恢复 Checkpoint")
        now = datetime.now(timezone.utc)
        due = deadline_at or now + timedelta(seconds=BACKGROUND_TASK_DEADLINE_S)
        digest = self._digest(target, thread_id, task_type, input_masked)
        # 任务表只存恢复引用与摘要，原始问题和地址均不得进入可查询任务记录。
        safe_input = {
            "checkpoint_ref": checkpoint_ref,
            "request_digest": digest,
            "request_id": trace.request_id,
            "correlation_id": trace.correlation_id,
            "trace_id": trace.trace_id,
        }
        task_id = f"task-{uuid.uuid4().hex}"
        event = parse_event(
            {
                "schema_version": "1.0",
                "event_id": f"evt-{uuid.uuid4().hex}",
                "event_type": "agent.task.created",
                "event_version": 1,
                "tenant_id": auth.tenant_id,
                "subject_id": auth.subject_id,
                "request_id": trace.request_id,
                "correlation_id": trace.correlation_id,
                "trace_id": trace.trace_id,
                "operation_id": task_id,
                "causation_id": None,
                "parent_event_id": None,
                "producer": "agent-service",
                "occurred_at": now.isoformat(),
                "retry_count": 0,
                "idempotency_key": idempotency_key,
                "payload": {
                    "task_id": task_id,
                    "task_type": task_type,
                    "checkpoint_ref": checkpoint_ref,
                    "actor_subject_id": auth.subject_id,
                    "customer_id": target,
                },
            }
        )
        return self._repository.create(
            tenant_id=auth.tenant_id,
            task_id=task_id,
            customer_id=target,
            actor_subject_id=auth.subject_id,
            thread_id=thread_id,
            task_type=task_type,
            input_masked=safe_input,
            input_digest=digest,
            checkpoint_ref=checkpoint_ref,
            idempotency_key=idempotency_key,
            deadline_at=due,
            event=event,
        )

    def find_existing(
        self,
        *,
        auth: AuthContext,
        task_type: str,
        input_masked: dict,
        thread_id: str,
        idempotency_key: str,
        customer_id: str,
    ) -> TaskRecord | None:
        require_customer_access(auth, customer_id)
        existing = self._repository.get_by_idempotency(
            auth.tenant_id, auth.subject_id, task_type, idempotency_key
        )
        if existing is None:
            return None
        if existing.input_digest != self._digest(customer_id, thread_id, task_type, input_masked):
            raise TaskConflictError("同一任务幂等键对应不同请求内容")
        require_customer_access(auth, existing.customer_id)
        return existing

    @staticmethod
    def _digest(customer_id: str, thread_id: str, task_type: str, input_masked: dict) -> str:
        return hashlib.sha256(
            json.dumps(
                [customer_id, thread_id, task_type, input_masked],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def get(self, auth: AuthContext, task_id: str) -> TaskRecord:
        require_tenant(auth, auth.tenant_id)
        if not task_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        record = self._repository.get(auth.tenant_id, task_id)
        if record is None or record.actor_subject_id != auth.subject_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, record.customer_id)
        return record
