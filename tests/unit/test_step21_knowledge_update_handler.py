"""事件清单摘要、文档版本和变更类型均经校验后才登记。"""

import hashlib

import pytest

from application.common.events import (
    EventContractViolation,
    EventEnvelope,
    KnowledgeUpdatedPayload,
)
from application.knowledge.retrieval.ports import KnowledgeChunk
from infrastructure.messaging.knowledge_update import knowledge_updated_handler

RAW = b'{"knowledge_release_id":"r1"}'
DIGEST = hashlib.sha256(RAW).hexdigest()
CHUNK = KnowledgeChunk(
    "t1", "r1", "doc1:v1:0001", "doc1", "ev1", 1, "Title", "Content", "Title Content", "published"
)


class Source:
    def load(self, tenant_id, release_id):
        return RAW, (CHUNK,)


class Repository:
    def __init__(self):
        self.calls = []

    def register_candidate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "registered"


def _event(*, digest=DIGEST, version="1", change_type="upsert"):
    return EventEnvelope(
        "1.0",
        "event-1",
        "knowledge.updated",
        1,
        "t1",
        "request-1",
        "corr-1",
        "trace-1",
        None,
        None,
        None,
        "knowledge-publisher",
        "2026-10-21T00:00:00Z",
        "idem-1",
        None,
        0,
        KnowledgeUpdatedPayload("r1", "doc1", version, change_type, digest, "system", None),
    )


def test_handler_registers_in_supplied_inbox_transaction() -> None:
    repository = Repository()
    connection = object()
    result = knowledge_updated_handler(repository, Source(), source_ref="reviewed")(
        connection, _event()
    )
    assert result == "r1"
    assert repository.calls[0][1]["connection"] is connection


@pytest.mark.parametrize(
    "event",
    [
        _event(digest="0" * 64),
        _event(version="2"),
        _event(change_type="delete"),
    ],
)
def test_handler_rejects_conflicting_update_without_registration(event) -> None:
    repository = Repository()
    with pytest.raises(ValueError):
        knowledge_updated_handler(repository, Source(), source_ref="reviewed")(object(), event)
    assert repository.calls == []


def test_payload_rejects_non_contract_change_type_and_digest() -> None:
    with pytest.raises(EventContractViolation):
        _event(change_type="updated")
    with pytest.raises(EventContractViolation):
        _event(digest="wrong")
