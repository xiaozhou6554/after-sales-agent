"""Ticket SQL adapter contract against an isolated transactional SQL database."""

from __future__ import annotations

from dataclasses import replace

import pytest
from migrations.versions.v0013_ticket_persistence import VERSION, statements
from sqlalchemy import create_engine, text

from domain.ticket.model import (
    Ticket,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    TicketVersionConflict,
    TicketVisibility,
)
from infrastructure.persistence.mysql.repositories.ticket_repository import (
    MySQLTicketRepository,
    TicketPersistenceConflict,
)


@pytest.fixture
def repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    # Portable subset only: migration-specific MySQL DDL is asserted separately.
    with engine.begin() as db:
        db.execute(
            text("""CREATE TABLE ticket (
            tenant_id TEXT NOT NULL, ticket_id TEXT NOT NULL, customer_id TEXT NOT NULL,
            created_by TEXT NOT NULL, title TEXT NOT NULL, description_masked TEXT NOT NULL,
            priority TEXT NOT NULL, status TEXT NOT NULL, assigned_to TEXT,
            revision INTEGER NOT NULL, context_snapshot TEXT NOT NULL, thread_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL, trace_id TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id,ticket_id))""")
        )
        db.execute(
            text("""CREATE TABLE ticket_message (
            tenant_id TEXT NOT NULL, message_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
            author_subject_id TEXT NOT NULL, content_masked TEXT NOT NULL,
            visibility TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id,message_id))""")
        )
        db.execute(
            text("""CREATE TABLE ticket_write_idempotency (
            tenant_id TEXT NOT NULL, actor_subject_id TEXT NOT NULL,
            operation_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL, ticket_id TEXT NOT NULL,
            result_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id,actor_subject_id,operation_type,idempotency_key))""")
        )
    try:
        yield MySQLTicketRepository(engine)
    finally:
        engine.dispose()


def item(tenant: str = "tenant-a", ticket_id: str = "ticket-1") -> Ticket:
    return Ticket(
        tenant,
        ticket_id,
        "customer-1",
        "subject-1",
        "设备故障",
        "设备无法启动",
        TicketPriority.NORMAL,
    )


def context(priority: str = "normal") -> dict:
    return {
        "question_masked": "设备无法启动",
        "summary": "设备故障待处理",
        "agent_results": ["已查询保修状态"],
        "tool_result_summaries": ["保修状态待核实"],
        "failure_reason": None,
        "business_refs": {"order_id": "ORDER-1"},
        "current_state": "open",
        "priority": priority,
        "next_action": "客服核实",
        "source_message_ids": ["msg-source-1"],
    }


def create(repo: MySQLTicketRepository, ticket: Ticket | None = None, key: str = "create-1"):
    return repo.create(
        ticket or item(),
        context_snapshot=context(),
        thread_id="thread-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        idempotency_key=key,
    )


def test_migration_version_and_tenant_keys() -> None:
    assert VERSION == "0013"
    ddl = statements()
    assert len(ddl) == 5
    assert all(sql.startswith("CREATE TABLE IF NOT EXISTS ") and ";" not in sql for sql in ddl)
    assert "PRIMARY KEY (tenant_id, ticket_id)" in ddl[0]
    assert "context_snapshot JSON NOT NULL" in ddl[0]
    assert "FOREIGN KEY (tenant_id, ticket_id)" in ddl[1]
    assert "FOREIGN KEY (tenant_id, ticket_id)" in ddl[3]
    assert "PRIMARY KEY (tenant_id, actor_subject_id, operation_type, idempotency_key)" in ddl[4]


def test_create_replay_and_tenant_isolation(repository: MySQLTicketRepository) -> None:
    assert create(repository).revision == 1
    assert create(repository).replayed
    assert repository.get("tenant-b", "ticket-1") is None
    assert repository.get("tenant-a", "ticket-1").ticket == item()
    assert create(repository, item("tenant-b")).replayed is False
    assert repository.get("tenant-b", "ticket-1").ticket.tenant_id == "tenant-b"
    with pytest.raises(TicketPersistenceConflict):
        create(repository, item(ticket_id="ticket-other"))
    assert repository.get("tenant-a", "ticket-other") is None


def test_revision_cas_update_and_idempotent_replay(repository: MySQLTicketRepository) -> None:
    create(repository)
    changed = item().change_priority(TicketPriority.HIGH, expected_revision=1)
    args = dict(
        expected_revision=1,
        actor_subject_id="subject-1",
        context_snapshot=context("high"),
        idempotency_key="update-1",
    )
    assert repository.save(changed, **args).revision == 2
    assert repository.save(changed, **args).replayed
    assert repository.get("tenant-a", "ticket-1").ticket.priority is TicketPriority.HIGH
    stale = item().change_status(TicketStatus.CLOSED, expected_revision=1)
    with pytest.raises(TicketVersionConflict):
        repository.save(
            stale,
            expected_revision=1,
            actor_subject_id="subject-1",
            context_snapshot=context(),
            idempotency_key="update-2",
        )
    with pytest.raises(TicketPersistenceConflict):
        repository.save(
            stale,
            expected_revision=1,
            actor_subject_id="subject-1",
            context_snapshot=context(),
            idempotency_key="update-1",
        )


def test_concurrent_same_key_commit_replays_after_cas_loss(
    repository: MySQLTicketRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(repository)
    changed = item().change_priority(TicketPriority.HIGH, expected_revision=1)
    args = dict(
        expected_revision=1,
        actor_subject_id="subject-1",
        context_snapshot=context("high"),
        idempotency_key="same-key",
    )
    assert repository.save(changed, **args).revision == 2
    original = repository._replay
    calls = 0

    def delayed_visibility(*values):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original(*values)

    monkeypatch.setattr(repository, "_replay", delayed_visibility)
    result = repository.save(changed, **args)
    assert result.replayed and result.revision == 2
    assert calls == 2


def test_messages_visibility_and_failed_update_rollback(repository: MySQLTicketRepository) -> None:
    create(repository)
    internal = TicketMessage("message-1", "staff-1", "内部处理备注", TicketVisibility.INTERNAL)
    changed = item().add_message(internal, expected_revision=1)
    repository.save(
        changed,
        expected_revision=1,
        actor_subject_id="staff-1",
        context_snapshot=context(),
        idempotency_key="update-internal",
    )
    assert repository.get("tenant-a", "ticket-1").ticket.messages == ()
    assert repository.get("tenant-a", "ticket-1", include_internal=True).ticket.messages == (
        internal,
    )
    tampered = replace(
        changed.change_priority(TicketPriority.HIGH, expected_revision=2), messages=()
    )
    with pytest.raises(TicketPersistenceConflict):
        repository.save(
            tampered,
            expected_revision=2,
            actor_subject_id="staff-1",
            context_snapshot=context("high"),
            idempotency_key="update-tamper",
        )
    stored = repository.get("tenant-a", "ticket-1", include_internal=True).ticket
    assert stored.revision == 2 and stored.priority is TicketPriority.NORMAL
    assert stored.messages == (internal,)


def test_context_is_structured_bounded_and_preserves_linkage(
    repository: MySQLTicketRepository,
) -> None:
    invalid = context()
    invalid["secret_extra"] = "not allowed"
    with pytest.raises(ValueError):
        repository.create(
            item(),
            context_snapshot=invalid,
            thread_id="thread-1",
            correlation_id="corr-1",
            trace_id="trace-1",
            idempotency_key="bad",
        )
    assert repository.get("tenant-a", "ticket-1") is None
    create(repository)
    stored = repository.get("tenant-a", "ticket-1")
    assert stored.context_snapshot == context()
    assert (stored.thread_id, stored.correlation_id, stored.trace_id) == (
        "thread-1",
        "corr-1",
        "trace-1",
    )
