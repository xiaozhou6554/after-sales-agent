"""步骤08 审计用例与链路传播单元测试：事前意图/事后结果、失败阻断、上下文隔离。"""

from __future__ import annotations

import logging

import pytest

from application.common.audit import AuditUseCase
from application.common.context import TraceContext
from application.common.ports.audit import (
    AuditAction,
    AuditCompletion,
    AuditIntent,
    AuditPersistenceError,
)
from application.common.tracing import (
    SubjectFrame,
    bind_request_context,
    current_subject,
    current_trace,
    reset_request_context,
)


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.intents: list[AuditIntent] = []
        self.completions: list[AuditCompletion] = []
        self.actions: list[AuditAction] = []
        self.fail = fail

    def record_intent(self, intent: AuditIntent) -> None:
        if self.fail:
            raise AuditPersistenceError("db down")
        self.intents.append(intent)

    def append_result(self, completion: AuditCompletion) -> None:
        if self.fail:
            raise AuditPersistenceError("db down")
        self.completions.append(completion)

    def record_action(self, action: AuditAction) -> None:
        if self.fail:
            raise AuditPersistenceError("db down")
        self.actions.append(action)


def _ids() -> dict:
    return {
        "tenant_id": "tenant-north",
        "actor_subject_id": "customer-north-01",
        "action": "tool.query_order",
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "trace_id": "trace-1",
    }


def test_tool_call_flow_persists_masked_intent_then_result() -> None:
    sink = FakeSink()
    use_case = AuditUseCase(sink)
    audit_id = use_case.begin_tool_call(
        **_ids(),
        customer_id="CUSTOMER-01",
        resource_refs=("order:ORDER-001",),
        parameter_summary={"order_id": "ORDER-001", "callback_phone": "13812345678"},
    )
    assert len(sink.intents) == 1 and len(sink.completions) == 0
    stored = sink.intents[0]
    # 入库前脱敏：电话键值整体掩码（16.2/543 行）。
    assert stored.parameter_summary["callback_phone"] == "***"
    assert stored.parameter_summary["order_id"] == "ORDER-001"
    assert stored.status == "pending"

    use_case.complete_tool_call(
        audit_id=audit_id,
        tenant_id="tenant-north",
        result_summary={"raw_response": "卡号 6222020200112233445"},
        duration_ms=12,
        status="success",
    )
    completion = sink.completions[0]
    assert "6222020200112233445" not in str(completion.result_summary)
    assert completion.duration_ms == 12


def test_intent_persistence_failure_blocks_high_risk_writes() -> None:
    sink = FakeSink(fail=True)
    use_case = AuditUseCase(sink)
    with pytest.raises(AuditPersistenceError):
        use_case.begin_tool_call(**_ids(), parameter_summary={})
    # 高风险写入调用方据 AuditPersistenceError 拒绝执行（20.3）。


def test_security_event_recorded_as_single_final_action() -> None:
    sink = FakeSink()
    use_case = AuditUseCase(sink)
    audit_id = use_case.record_action(
        **_ids(),
        status="denied",
        result_summary={"reason": "customer out of scope"},
    )
    assert audit_id
    assert len(sink.actions) == 1 and not sink.intents
    assert sink.actions[0].status == "denied"


def test_trace_context_binds_and_resets_isolated() -> None:
    trace = TraceContext("1.0", "req-1", "corr-1", "trace-1", thread_id="thread-1")
    tokens = bind_request_context(trace, SubjectFrame("tenant-north", "customer-north-01"))
    try:
        assert current_trace() is trace
        assert current_subject() == SubjectFrame("tenant-north", "customer-north-01")
    finally:
        reset_request_context(tokens)
    assert current_trace() is None
    assert current_subject() is None


def test_trace_context_nested_binding_restores_outer(caplog) -> None:
    outer = TraceContext("1.0", "req-a", "corr-a", "trace-a")
    tokens = bind_request_context(outer)
    try:
        inner = TraceContext("1.0", "req-b", "corr-b", "trace-b")
        inner_tokens = bind_request_context(inner)
        try:
            assert current_trace() is inner
        finally:
            reset_request_context(inner_tokens)
        assert current_trace() is outer
    finally:
        reset_request_context(tokens)


def test_json_log_formatter_carries_trace_and_masks_nothing(caplog) -> None:
    from infrastructure.observability.logging_setup import JsonFormatter, TraceContextFilter

    trace = TraceContext("1.0", "req-9", "corr-9", "trace-9")
    tokens = bind_request_context(trace, SubjectFrame("tenant-north", "support-north-01"))
    try:
        with caplog.at_level(logging.INFO, logger="after_sales.test"):
            logging.getLogger("after_sales.test").info(
                "工具调用完成", extra={"details": {"tool": "query_order"}}
            )
        record = caplog.records[-1]
        for filter_instance in (TraceContextFilter(),):
            assert filter_instance.filter(record)
        payload = JsonFormatter().format(record)
        assert '"trace_id": "trace-9"' in payload
        assert '"tenant_id": "tenant-north"' in payload
        assert '"subject_id": "support-north-01"' in payload
        assert "query_order" in payload
    finally:
        reset_request_context(tokens)
