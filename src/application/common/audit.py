"""审计用例：统一脱敏入口与事前意图/事后结果语义（技术详细规格 20.1、20.3）。

- 写入审计前对参数与结果摘要统一脱敏（16.2/543 行：入库前必须脱敏）；
- begin_tool_call 先持久化调用意图；意图持久化失败抛 AuditPersistenceError，
  高风险写入必须据此拒绝；
- complete_tool_call 在工具结束后追加结果；不因审计补写重执行业务。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from application.common.masking import mask_mapping
from application.common.ports.audit import (
    AuditAction,
    AuditCompletion,
    AuditIntent,
    AuditSink,
)

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class AuditUseCase:
    def __init__(
        self,
        sink: AuditSink,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def begin_tool_call(
        self,
        *,
        tenant_id: str,
        actor_subject_id: str,
        action: str,
        request_id: str,
        correlation_id: str,
        trace_id: str,
        thread_id: str | None = None,
        customer_id: str | None = None,
        resource_refs: tuple[str, ...] = (),
        parameter_summary: dict | None = None,
    ) -> str:
        """事前审计：持久化调用意图并返回 audit_id；失败必须阻断高风险写入。"""
        audit_id = self._id_factory()
        intent = AuditIntent(
            audit_id=audit_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            thread_id=thread_id,
            actor_subject_id=actor_subject_id,
            customer_id=customer_id,
            action=action,
            resource_refs=tuple(resource_refs),
            parameter_summary=mask_mapping(parameter_summary or {}),
        )
        self._sink.record_intent(intent)
        return audit_id

    def complete_tool_call(
        self,
        *,
        audit_id: str,
        tenant_id: str,
        result_summary: dict | None = None,
        duration_ms: int | None = None,
        status: str = STATUS_SUCCESS,
        error_code: str | None = None,
        event_id: str | None = None,
    ) -> None:
        completion = AuditCompletion(
            audit_id=audit_id,
            tenant_id=tenant_id,
            result_summary=mask_mapping(result_summary or {}),
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            event_id=event_id,
        )
        self._sink.append_result(completion)

    def record_action(
        self,
        *,
        tenant_id: str,
        actor_subject_id: str,
        action: str,
        request_id: str,
        correlation_id: str,
        trace_id: str,
        thread_id: str | None = None,
        customer_id: str | None = None,
        resource_refs: tuple[str, ...] = (),
        parameter_summary: dict | None = None,
        result_summary: dict | None = None,
        duration_ms: int | None = None,
        status: str,
        error_code: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """单条终态审计（安全事件、业务动作）；返回 audit_id 便于链路关联。"""
        audit_id = self._id_factory()
        self._sink.record_action(
            AuditAction(
                audit_id=audit_id,
                tenant_id=tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
                thread_id=thread_id,
                actor_subject_id=actor_subject_id,
                customer_id=customer_id,
                action=action,
                resource_refs=tuple(resource_refs),
                parameter_summary=mask_mapping(parameter_summary or {}),
                result_summary=mask_mapping(result_summary or {}),
                duration_ms=duration_ms,
                status=status,
                error_code=error_code,
                event_id=event_id,
            )
        )
        return audit_id
