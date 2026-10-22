"""异步对话受理：幂等核对、暂停点准备和任务创建。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from application.agent.service import ChatService
from application.common.authorization import require_customer_access
from application.common.context import AuthContext, TraceContext
from application.common.dto import ChatRequestDto
from application.common.tasks.models import TaskRecord
from application.common.tasks.use_cases import TaskUseCases


@dataclass(frozen=True)
class AcceptedChatTask:
    task: TaskRecord
    trace: TraceContext


class ChatTaskSubmission:
    def __init__(self, chat: ChatService, tasks: TaskUseCases) -> None:
        self._chat = chat
        self._tasks = tasks

    def submit(
        self, request: ChatRequestDto, auth: AuthContext, idempotency_key: str
    ) -> AcceptedChatTask:
        if not 16 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency-Key 长度必须为 16 至 128")
        customer_id = request.customer_id
        if customer_id is None:
            allowed = auth.data_scope.customer_ids
            if len(allowed) != 1 or allowed[0] == "*":
                raise ValueError("代客对话必须指定目标客户")
            customer_id = allowed[0]
        require_customer_access(auth, customer_id)
        stable_name = f"{auth.tenant_id}:{auth.subject_id}:{idempotency_key}"
        thread_id = request.thread_id or f"thread-{uuid.uuid5(uuid.NAMESPACE_URL, stable_name).hex}"
        digest = hashlib.sha256(
            json.dumps(
                {
                    "message": request.message,
                    "business_refs": [vars(ref) for ref in request.business_refs],
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        input_masked = {"request_digest": digest}
        existing = self._tasks.find_existing(
            auth=auth,
            task_type="agent.chat",
            input_masked=input_masked,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            customer_id=customer_id,
        )
        if existing is not None:
            return AcceptedChatTask(existing, self._trace(request.trace, existing.thread_id))
        trace = self._trace(request.trace, thread_id)
        checkpoint_ref = self._chat.prepare_async(
            request, auth, thread_id=thread_id, idempotency_key=idempotency_key
        )
        task = self._tasks.create(
            auth=auth,
            trace=trace,
            task_type="agent.chat",
            input_masked=input_masked,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            customer_id=customer_id,
            checkpoint_ref=checkpoint_ref,
        )
        return AcceptedChatTask(task, trace)

    @staticmethod
    def _trace(source: TraceContext, thread_id: str) -> TraceContext:
        return TraceContext(
            schema_version=source.schema_version,
            request_id=source.request_id,
            correlation_id=source.correlation_id,
            trace_id=source.trace_id,
            thread_id=thread_id,
        )
