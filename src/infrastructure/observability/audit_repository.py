"""MySQL 审计仓储：agent_audit_log 的事前意图/事后结果与单条终态写入。

表结构见 migrations/versions/v0003_audit_table.py（25 章审计字段全集）：
追加不可变，tenant+audit_id 主键，event_id 存在时 tenant+event_id 唯一。
任何持久化失败都转换为 AuditPersistenceError，由调用方拒绝高风险写入。
"""

from __future__ import annotations

import json

from sqlalchemy import text

from application.common.ports.audit import (
    AuditAction,
    AuditCompletion,
    AuditIntent,
    AuditPersistenceError,
)

_INSERT_INTENT_SQL = text(
    """
    INSERT INTO agent_audit_log (
        audit_id, tenant_id, event_id, request_id, correlation_id, trace_id, thread_id,
        actor_subject_id, customer_id, action, resource_refs, parameter_summary,
        result_summary, duration_ms, status, error_code
    ) VALUES (
        :audit_id, :tenant_id, NULL, :request_id, :correlation_id, :trace_id, :thread_id,
        :actor_subject_id, :customer_id, :action, :resource_refs, :parameter_summary,
        NULL, NULL, 'pending', NULL
    )
    """
)

_APPEND_RESULT_SQL = text(
    """
    UPDATE agent_audit_log
    SET result_summary = :result_summary,
        duration_ms = :duration_ms,
        status = :status,
        error_code = :error_code,
        event_id = COALESCE(:event_id, event_id)
    WHERE tenant_id = :tenant_id AND audit_id = :audit_id AND status = 'pending'
    """
)

_INSERT_ACTION_SQL = text(
    """
    INSERT INTO agent_audit_log (
        audit_id, tenant_id, event_id, request_id, correlation_id, trace_id, thread_id,
        actor_subject_id, customer_id, action, resource_refs, parameter_summary,
        result_summary, duration_ms, status, error_code
    ) VALUES (
        :audit_id, :tenant_id, :event_id, :request_id, :correlation_id, :trace_id, :thread_id,
        :actor_subject_id, :customer_id, :action, :resource_refs, :parameter_summary,
        :result_summary, :duration_ms, :status, :error_code
    )
    """
)


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


class MySQLAuditRepository:
    """实现 AuditSink；同一连接事务内写入，失败转 AuditPersistenceError。"""

    def __init__(self, engine) -> None:
        self._engine = engine

    def record_intent(self, intent: AuditIntent) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    _INSERT_INTENT_SQL,
                    {
                        "audit_id": intent.audit_id,
                        "tenant_id": intent.tenant_id,
                        "request_id": intent.request_id,
                        "correlation_id": intent.correlation_id,
                        "trace_id": intent.trace_id,
                        "thread_id": intent.thread_id,
                        "actor_subject_id": intent.actor_subject_id,
                        "customer_id": intent.customer_id,
                        "action": intent.action,
                        "resource_refs": _json(list(intent.resource_refs)),
                        "parameter_summary": _json(intent.parameter_summary),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 任何失败都按审计不可用处理
            raise AuditPersistenceError(f"审计意图持久化失败: {exc}") from exc

    def append_result(self, completion: AuditCompletion) -> None:
        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    _APPEND_RESULT_SQL,
                    {
                        "audit_id": completion.audit_id,
                        "tenant_id": completion.tenant_id,
                        "result_summary": _json(completion.result_summary),
                        "duration_ms": completion.duration_ms,
                        "status": completion.status,
                        "error_code": completion.error_code,
                        "event_id": completion.event_id,
                    },
                )
            if result.rowcount == 0:
                raise AuditPersistenceError(
                    f"审计结果追加失败：意图不存在或已完成 audit_id={completion.audit_id}"
                )
        except AuditPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuditPersistenceError(f"审计结果持久化失败: {exc}") from exc

    def record_action(self, action: AuditAction) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    _INSERT_ACTION_SQL,
                    {
                        "audit_id": action.audit_id,
                        "tenant_id": action.tenant_id,
                        "event_id": action.event_id,
                        "request_id": action.request_id,
                        "correlation_id": action.correlation_id,
                        "trace_id": action.trace_id,
                        "thread_id": action.thread_id,
                        "actor_subject_id": action.actor_subject_id,
                        "customer_id": action.customer_id,
                        "action": action.action,
                        "resource_refs": _json(list(action.resource_refs)),
                        "parameter_summary": _json(action.parameter_summary),
                        "result_summary": _json(action.result_summary),
                        "duration_ms": action.duration_ms,
                        "status": action.status,
                        "error_code": action.error_code,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            raise AuditPersistenceError(f"审计记录持久化失败: {exc}") from exc
