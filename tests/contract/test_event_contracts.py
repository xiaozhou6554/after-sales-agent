"""事件契约测试：信封严格解析、18 类事件类型齐全、载荷联合校验。"""

from __future__ import annotations

import copy

import pytest

from application.common.events import (
    EVENT_PAYLOAD_REGISTRY,
    EVENT_TYPES,
    EventContractViolation,
    parse_event,
)
from tests.contract.samples import EVENT_ENVELOPE_TICKET_CREATED


def test_registry_covers_all_declared_event_types() -> None:
    assert set(EVENT_PAYLOAD_REGISTRY) == set(EVENT_TYPES)
    assert len(EVENT_PAYLOAD_REGISTRY) == 18


def test_sample_event_parses_with_typed_payload() -> None:
    envelope = parse_event(copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED))
    assert envelope.event_type == "ticket.created"
    assert envelope.payload.ticket_id == "TCK-0001"
    assert envelope.payload.customer_id == "CUSTOMER-01"
    assert envelope.payload_dict()["priority"] == "normal"


def test_unknown_event_type_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["event_type"] = "ticket.deleted_forever"
    with pytest.raises(EventContractViolation, match="未知 event_type"):
        parse_event(sample)


def test_unknown_envelope_field_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["debug_note"] = "internal"
    with pytest.raises(EventContractViolation, match="未知字段"):
        parse_event(sample)


def test_unknown_payload_field_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    # ticket.created 载荷不得携带用户问题原文。
    sample["payload"]["question_raw"] = "用户完整问题原文……"
    with pytest.raises(EventContractViolation, match="未知字段"):
        parse_event(sample)


def test_missing_payload_field_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    del sample["payload"]["context_ref"]
    with pytest.raises(EventContractViolation, match="构造失败"):
        parse_event(sample)


def test_nullable_payload_field_must_be_explicit() -> None:
    # 可空字段缺失与显式 null 不同：缺失拒绝，显式 null 接受。
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    del sample["payload"]["ticket_id"]
    with pytest.raises(EventContractViolation):
        parse_event(sample)

    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["payload"]["ticket_id"] = None
    assert parse_event(sample).payload.ticket_id is None


def test_unknown_schema_version_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["schema_version"] = "2.0"
    with pytest.raises(EventContractViolation, match="schema_version"):
        parse_event(sample)


def test_idempotency_key_length_enforced() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["idempotency_key"] = "short"
    with pytest.raises(EventContractViolation, match="idempotency_key"):
        parse_event(sample)


def test_invalid_occurred_at_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["occurred_at"] = "yesterday"
    with pytest.raises(EventContractViolation, match="occurred_at"):
        parse_event(sample)


def test_payload_field_type_violations_rejected() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["payload"]["revision"] = -1
    with pytest.raises(EventContractViolation, match="非负整数"):
        parse_event(sample)

    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["payload"]["status"] = ""
    with pytest.raises(EventContractViolation, match="非空字符串"):
        parse_event(sample)


def test_refund_execution_succeeded_requires_iso_confirmed_at() -> None:
    sample = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    sample["event_type"] = "refund.execution.succeeded"
    sample["payload"] = {
        "execution_id": "EXEC-1",
        "refund_request_id": "REF-1",
        "channel_transaction_ref": "PSP-X-1",
        "amount_minor": 10000,
        "currency": "CNY",
        "confirmed_at": "2026-10-20T08:00:00Z",
        "source_version": "pay-v1",
        "environment": "mock",
        "actor_subject_id": "payment-service",
        "customer_id": "CUSTOMER-01",
    }
    envelope = parse_event(sample)
    assert envelope.payload.amount_minor == 10000

    sample["payload"]["confirmed_at"] = "not-a-time"
    with pytest.raises(EventContractViolation, match="confirmed_at"):
        parse_event(sample)
