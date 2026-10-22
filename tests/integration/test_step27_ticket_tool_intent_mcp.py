"""MCP write-tool wiring: model fields only, trusted state drives the write.

``create_ticket``/``update_ticket`` resolve the registered-and-verified intent
from server state (confirmation, idempotency, trace); without it they fail
closed. ``handoff_to_human`` needs no confirmation and replays by derived key.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from bootstrap.container import MCP_TOOLS_PATH
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.common.context import TraceContext
from application.common.masking import mask_text
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from application.ticket.handoff import MockHumanHandoff
from application.ticket.service import TicketApplicationService, TicketContextSnapshot
from domain.ticket import TicketPriority, TicketVisibility
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.ticket.mysql_confirmed_writer import MySQLConfirmedTicketWriter
from infrastructure.ticket.mysql_handoff_queue import MySQLHandoffQueue
from infrastructure.ticket.mysql_tool_intent import MySQLTicketToolIntentStore
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

TENANT = "tenant-north"
CUSTOMER = "CUSTOMER-01"
DESCRIPTION = "机顶盒无法开机，报错E4"
FOLLOWUP = "已按指引重启，仍然无法开机"
CONFIRMATIONS_TTL_S = 300


@pytest.fixture
def mcp_env():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    marker = uuid.uuid4().hex[:10]
    auth_gateway = MockAuthAdapter.from_fixture("data/fixtures/mock-identities.json", enabled=True)
    grant = auth_gateway.issue_mock_session("cust-north-01")
    confirmations = MySQLConfirmationStore(engine)
    tickets = TicketApplicationService(
        MySQLTicketRepository(engine), MySQLConfirmedTicketWriter(engine), confirmations
    )
    deps = MCPToolDeps(
        auth_gateway=auth_gateway,
        guard=ToolContextGuard(MCP_TOOLS_PATH),
        query_order=QueryOrderUseCase(MockOrderAdapter(engine)),
        query_payment=QueryPaymentUseCase(MockOrderAdapter(engine)),
        audit=AuditUseCase(MySQLAuditRepository(engine)),
        ticket_query=tickets,
        ticket_tool_intent=MySQLTicketToolIntentStore(engine, tickets),
        human_handoff=MockHumanHandoff(MySQLHandoffQueue(engine)),
    )
    try:
        yield engine, tickets, confirmations, deps, grant, marker
    finally:
        like = f"%{marker}%"
        with engine.begin() as connection:
            confirmation_ids = [
                row["confirmation_id"]
                for row in connection.execute(
                    text(
                        "SELECT confirmation_id FROM ticket_tool_intent"
                        " WHERE tenant_id=:tenant"
                        " AND ticket_id IN (SELECT ticket_id FROM ticket"
                        " WHERE tenant_id=:tenant AND title LIKE :m)"
                    ),
                    {"tenant": TENANT, "m": like},
                ).mappings()
            ]
            for statement, params in (
                (
                    "DELETE FROM consumer_inbox WHERE tenant_id=:tenant AND event_id IN"
                    " (SELECT event_id FROM outbox_event WHERE tenant_id=:tenant"
                    " AND payload LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM outbox_event WHERE tenant_id=:tenant AND payload LIKE :m",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM tool_invocation WHERE tenant_id=:tenant AND args_summary LIKE :m",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM agent_audit_log WHERE tenant_id=:tenant"
                    " AND parameter_summary LIKE :m",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM ticket_tool_intent WHERE tenant_id=:tenant"
                    " AND ticket_id IN (SELECT ticket_id FROM ticket"
                    " WHERE tenant_id=:tenant AND title LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM ticket_message WHERE tenant_id=:tenant"
                    " AND ticket_id IN (SELECT ticket_id FROM ticket"
                    " WHERE tenant_id=:tenant AND title LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM ticket_write_idempotency WHERE tenant_id=:tenant"
                    " AND ticket_id IN (SELECT ticket_id FROM ticket"
                    " WHERE tenant_id=:tenant AND title LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM human_handoff_claim WHERE tenant_id=:tenant"
                    " AND handoff_id IN (SELECT handoff_id FROM human_handoff"
                    " WHERE tenant_id=:tenant AND reason_code LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM human_handoff_idempotency WHERE tenant_id=:tenant"
                    " AND handoff_id IN (SELECT handoff_id FROM human_handoff"
                    " WHERE tenant_id=:tenant AND reason_code LIKE :m)",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM human_handoff WHERE tenant_id=:tenant AND reason_code LIKE :m",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "DELETE FROM ticket WHERE tenant_id=:tenant AND title LIKE :m",
                    {"tenant": TENANT, "m": like},
                ),
                (
                    "UPDATE human_handoff_capacity SET active_count="
                    " (SELECT COUNT(*) FROM human_handoff"
                    "  WHERE tenant_id='tenant-north' AND status='queued')"
                    " WHERE tenant_id=:tenant",
                    {"tenant": TENANT},
                ),
            ):
                connection.execute(text(statement), params)
            for confirmation_id in confirmation_ids:
                connection.execute(
                    text(
                        "DELETE FROM operation_confirmation WHERE tenant_id=:tenant"
                        " AND confirmation_id=:cid"
                    ),
                    {"tenant": TENANT, "cid": confirmation_id},
                )
            leftovers = (
                connection.execute(
                    text("SELECT ticket_id FROM ticket WHERE tenant_id=:tenant AND title LIKE :m"),
                    {"tenant": TENANT, "m": like},
                )
                .mappings()
                .all()
            )
            assert not leftovers, "ticket cleanup left marker rows"
        engine.dispose()


def _headers(token: str, marker: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "x-request-id": f"req-{marker}",
        "x-correlation-id": f"corr-{marker}",
        "x-trace-id": f"trace-{marker}",
        "x-thread-id": "thread-mcp",
    }


def _context() -> TicketContextSnapshot:
    return TicketContextSnapshot(
        "masked issue",
        "masked summary",
        ("agent summary",),
        ("tool summary",),
        None,
        (("order_id", "ORDER-1"),),
        "open",
        "contact support",
    )


def _trace(marker: str) -> TraceContext:
    return TraceContext("1.0", f"req-{marker}", f"corr-{marker}", f"trace-{marker}", "thread-mcp")


def _prepare_and_verify(mcp_env, title: str) -> tuple[dict, str]:
    _, tickets, confirmations, _, grant, marker = mcp_env
    args = dict(
        customer_id=CUSTOMER,
        title=title,
        description_masked=mask_text(DESCRIPTION),
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(marker),
        idempotency_key=uuid.uuid4().hex,
    )
    prepared = tickets.prepare_create(grant.auth, **args)
    confirmation_id = prepared.confirmation.record.confirmation_id
    OperationConfirmationUseCase(confirmations, CONFIRMATIONS_TTL_S).verify(
        TENANT, confirmation_id, prepared.confirmation.mock_challenge
    )
    return args, confirmation_id


def _create_via_mcp(mcp_env, title: str) -> dict:
    _, _, _, deps, grant, marker = mcp_env
    args, confirmation_id = _prepare_and_verify(mcp_env, title)
    deps.ticket_tool_intent.register_create(grant.auth, confirmation_id=confirmation_id, **args)
    return execute_tool_call(
        "create_ticket",
        {"title": title, "description": DESCRIPTION, "priority": "normal"},
        deps=deps,
        headers=_headers(grant.session_token, marker),
    )


def test_create_ticket_via_mcp_requires_registered_confirmation(mcp_env) -> None:
    _, _, _, deps, grant, marker = mcp_env
    title = f"MCP-TK-{marker}"
    args, confirmation_id = _prepare_and_verify(mcp_env, title)
    visible = {"title": title, "description": DESCRIPTION, "priority": "normal"}
    headers = _headers(grant.session_token, marker)

    blind = execute_tool_call("create_ticket", dict(visible), deps=deps, headers=headers)
    assert blind["status"] == "error"
    assert blind["error_code"] == "CONFIRMATION_INVALID"

    deps.ticket_tool_intent.register_create(grant.auth, confirmation_id=confirmation_id, **args)
    created = execute_tool_call("create_ticket", dict(visible), deps=deps, headers=headers)
    assert created["status"] == "success"
    assert created["data"]["revision"] == 1
    assert created["data"]["replayed"] is False
    ticket_id = created["data"]["ticket_id"]

    replay = execute_tool_call("create_ticket", dict(visible), deps=deps, headers=headers)
    assert replay["status"] == "success"
    assert replay["data"]["ticket_id"] == ticket_id
    assert replay["data"]["replayed"] is True


def test_update_ticket_via_mcp_appends_customer_message(mcp_env) -> None:
    _, tickets, confirmations, deps, grant, marker = mcp_env
    title = f"MCP-TK-{marker}"
    created = _create_via_mcp(mcp_env, title)
    assert created["status"] == "success"
    ticket_id = created["data"]["ticket_id"]

    args = dict(
        ticket_id=ticket_id,
        content_masked=mask_text(FOLLOWUP),
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=_context(),
        idempotency_key=uuid.uuid4().hex,
    )
    prepared = tickets.prepare_update(grant.auth, **args)
    update_confirmation = prepared.confirmation.record.confirmation_id
    OperationConfirmationUseCase(confirmations, CONFIRMATIONS_TTL_S).verify(
        TENANT, update_confirmation, prepared.confirmation.mock_challenge
    )
    deps.ticket_tool_intent.register_update(
        grant.auth, confirmation_id=update_confirmation, trace=_trace(marker), **args
    )

    updated = execute_tool_call(
        "update_ticket",
        {"ticket_id": ticket_id, "content": FOLLOWUP},
        deps=deps,
        headers=_headers(grant.session_token, marker),
    )
    assert updated["status"] == "success"
    assert updated["data"]["revision"] == 2
    assert updated["data"]["replayed"] is False
    replay = execute_tool_call(
        "update_ticket",
        {"ticket_id": ticket_id, "content": FOLLOWUP},
        deps=deps,
        headers=_headers(grant.session_token, marker),
    )
    assert replay["data"]["revision"] == 2
    assert replay["data"]["replayed"] is True


def test_handoff_to_human_queues_replays_and_marks_unavailable(mcp_env) -> None:
    _, _, _, deps, grant, marker = mcp_env
    reason = f"UNRESOLVED-{marker}"
    headers = _headers(grant.session_token, marker)

    first = execute_tool_call(
        "handoff_to_human",
        {"reason": reason, "summary": "人工仍未接入"},
        deps=deps,
        headers=headers,
    )
    assert first["status"] == "success"
    assert first["data"]["handoff_status"] == "queued"
    handoff_id = first["data"]["handoff_id"]

    replay = execute_tool_call(
        "handoff_to_human",
        {"reason": reason, "summary": "人工仍未接入"},
        deps=deps,
        headers=headers,
    )
    assert replay["status"] == "success"
    assert replay["data"]["handoff_id"] == handoff_id

    unavailable_deps = replace(deps, human_handoff_available=False)
    partial = execute_tool_call(
        "handoff_to_human",
        {"reason": f"UNRESOLVED-DOWN-{marker}", "summary": "人工仍未接入"},
        deps=unavailable_deps,
        headers=headers,
    )
    assert partial["status"] == "partial"
    assert partial["data"]["handoff_status"] == "unavailable"
    assert "fallback_hint" in partial["data"]


def test_non_customer_subject_cannot_call_ticket_writes(mcp_env) -> None:
    _, _, _, deps, _, marker = mcp_env
    agent_grant = deps.auth_gateway.issue_mock_session("agent-north-01")
    response = execute_tool_call(
        "create_ticket",
        {"title": f"MCP-TK-{marker}", "description": DESCRIPTION, "priority": "normal"},
        deps=deps,
        headers=_headers(agent_grant.session_token, marker),
    )
    assert response["status"] == "error"
    assert response["error_code"] == "RESOURCE_UNAVAILABLE"
