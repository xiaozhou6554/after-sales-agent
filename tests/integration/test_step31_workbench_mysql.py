from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from application.common.context import AuthContext, DataScope
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.workbench.commands import WorkbenchTicketCommands
from application.workbench.queries import WorkbenchQueries
from domain.ticket import Ticket, TicketPriority
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.workbench.mock_staff_directory import MockStaffDirectory
from infrastructure.workbench.mysql_queries import (
    MySQLWorkbenchQueries,
    MySQLWorkbenchTicketCommand,
)


@pytest.fixture
def workbench_case():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"local MySQL unavailable: {exc.orig.args[0]}")
    tenant = "tenant-north"
    ticket_id = f"step31-{uuid.uuid4().hex}"
    customer_id = "CUSTOMER-01"
    try:
        yield tenant, customer_id, ticket_id, engine
    finally:
        with engine.begin() as connection:
            for table in ("ticket_write_idempotency", "ticket_message", "ticket"):
                connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE tenant_id=:tenant AND "
                        + (
                            "ticket_id=:ticket"
                            if table != "ticket_message"
                            else "ticket_id=:ticket"
                        )
                    ),
                    {"tenant": tenant, "ticket": ticket_id},
                )
            connection.execute(
                text(
                    "DELETE FROM operation_confirmation "
                    "WHERE tenant_id=:tenant AND target_id=:ticket"
                ),
                {"tenant": tenant, "ticket": ticket_id},
            )
        engine.dispose()


def _staff(tenant: str) -> AuthContext:
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant,
        "supervisor-north-01",
        ("supervisor",),
        ("customer:read", "ticket:read", "ticket:assign", "ticket:status"),
        DataScope(("CUSTOMER-01",), ("team-north-support", "team-north-front")),
        "step31-session",
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
    )


def test_workbench_queries_and_confirmed_ticket_commands_are_scoped_and_idempotent(workbench_case):
    tenant, customer_id, ticket_id, engine = workbench_case
    repository = MySQLTicketRepository(engine)
    snapshot = {
        "question_masked": "设备无法启动",
        "summary": "待客服处理",
        "agent_results": [],
        "tool_result_summaries": [],
        "failure_reason": None,
        "business_refs": {},
        "current_state": "open",
        "priority": "normal",
        "next_action": "人工处理",
        "source_message_ids": [],
    }
    repository.create(
        Ticket(
            tenant,
            ticket_id,
            customer_id,
            "customer-north-01",
            "设备故障",
            "无法启动",
            TicketPriority.NORMAL,
        ),
        context_snapshot=snapshot,
        thread_id="step31-thread",
        correlation_id="step31-corr",
        trace_id="step31-trace",
        idempotency_key=f"create-{ticket_id}",
    )
    auth = _staff(tenant)
    queries = WorkbenchQueries(MySQLWorkbenchQueries(engine))
    listed = queries.tickets(auth, customer_id=customer_id)
    assert any(item["ticket_id"] == ticket_id for item in listed["items"])
    assert queries.ticket_detail(auth, ticket_id)["messages"] == []

    confirmation_store = MySQLConfirmationStore(engine)
    confirmations = OperationConfirmationUseCase(confirmation_store, 300)
    commands = WorkbenchTicketCommands(
        queries, confirmations, MySQLWorkbenchTicketCommand(engine), MockStaffDirectory()
    )
    prepared = commands.prepare_assignment(auth, ticket_id, "human-agent-north-01", 1)
    commands.verify(auth, prepared.record.confirmation_id, prepared.mock_challenge)
    result = commands.assign(
        auth,
        ticket_id,
        "human-agent-north-01",
        1,
        prepared.record.confirmation_id,
        f"assign-{ticket_id}",
    )
    replay = commands.assign(
        auth,
        ticket_id,
        "human-agent-north-01",
        1,
        prepared.record.confirmation_id,
        f"assign-{ticket_id}",
    )
    assert result["revision"] == 2 and result["replayed"] is False
    assert replay["revision"] == 2 and replay["replayed"] is True

    status_confirmation = commands.prepare_status(
        auth, ticket_id, "in_progress", "CUSTOMER_REQUEST", 2
    )
    commands.verify(
        auth, status_confirmation.record.confirmation_id, status_confirmation.mock_challenge
    )
    changed = commands.change_status(
        auth,
        ticket_id,
        "in_progress",
        "CUSTOMER_REQUEST",
        2,
        status_confirmation.record.confirmation_id,
        f"status-{ticket_id}",
    )
    assert changed["revision"] == 3
    assert queries.ticket_detail(auth, ticket_id)["status"] == "in_progress"
