"""Real MySQL and MCP proof for the read-only ticket query boundary."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from bootstrap.settings import load_settings
from sqlalchemy import text

from domain.ticket import Ticket, TicketMessage, TicketPriority, TicketVisibility
from infrastructure.mcp.local_probe import MCPServerProcess, free_port, probe_trace
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository


def test_query_ticket_mcp_authorizes_and_projects_without_private_content() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    ticket_id = f"step27-mcp-{uuid.uuid4().hex}"
    tenant_id = "tenant-north"
    process = None
    try:
        ticket = Ticket(
            tenant_id,
            ticket_id,
            "CUSTOMER-01",
            "customer-north-01",
            "public-title-marker",
            "private-description-marker",
            TicketPriority.NORMAL,
            messages=(
                TicketMessage(
                    f"customer-{uuid.uuid4().hex}",
                    "customer-north-01",
                    "customer-message-marker",
                    TicketVisibility.CUSTOMER,
                ),
                TicketMessage(
                    f"internal-{uuid.uuid4().hex}",
                    "support-north-01",
                    "internal-message-marker",
                    TicketVisibility.INTERNAL,
                ),
            ),
        )
        MySQLTicketRepository(engine).create(
            ticket,
            context_snapshot={
                "question_masked": "private-question-marker",
                "summary": "private-context-marker",
                "agent_results": [],
                "tool_result_summaries": [],
                "failure_reason": None,
                "business_refs": {},
                "current_state": "open",
                "priority": "normal",
                "next_action": "review",
                "source_message_ids": [],
            },
            thread_id=f"thread-{uuid.uuid4().hex}",
            correlation_id=f"corr-{uuid.uuid4().hex}",
            trace_id=f"trace-{uuid.uuid4().hex}",
            idempotency_key=f"key-{uuid.uuid4().hex}",
        )
        process = MCPServerProcess(free_port())
        process.wait_ready()
        owner = process.login("cust-north-01")
        support = process.login("support-north-01")
        other_customer = process.login("cust-north-02")
        other_tenant = process.login("cust-south-91")
        for token in (owner, support):
            response = process.call(
                "query_ticket",
                {"ticket_id": ticket_id},
                token=token,
                trace=probe_trace("ticket-visible"),
            )
            assert response["status"] == "success", response
            assert response["data"]["found"] is True
            summary = response["data"]["ticket"]
            assert summary["ticket_id"] == ticket_id
            assert summary["status"] == "open"
            assert summary["priority"] == "normal"
            assert summary["revision"] == 1
            assert summary["assigned_to"] is None
            assert datetime.fromisoformat(summary["updated_at"]).tzinfo is not None
            assert set(summary) == {
                "ticket_id",
                "status",
                "priority",
                "revision",
                "assigned_to",
                "updated_at",
            }
            wire = json.dumps(response)
            for marker in (
                "private-description-marker",
                "customer-message-marker",
                "internal-message-marker",
                "private-question-marker",
                "private-context-marker",
            ):
                assert marker not in wire

        for token, requested_id in (
            (other_customer, ticket_id),
            (other_tenant, ticket_id),
            (owner, "missing-" + ticket_id),
        ):
            denied = process.call(
                "query_ticket",
                {"ticket_id": requested_id},
                token=token,
                trace=probe_trace("ticket-denied"),
            )
            assert denied["status"] == "error"
            assert denied["error_code"] == "RESOURCE_UNAVAILABLE"
        assert (
            process.call(
                "query_ticket",
                {"ticket_id": ticket_id},
                token=owner,
                trace=probe_trace("ticket-repeat"),
            )["data"]["ticket"]["revision"]
            == 1
        )
    finally:
        if process is not None:
            process.stop()
        with engine.begin() as connection:
            params = {"tenant_id": tenant_id, "ticket_id": ticket_id}
            for table in ("ticket_write_idempotency", "ticket_message", "ticket"):
                connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE tenant_id=:tenant_id AND ticket_id=:ticket_id"
                    ),
                    params,
                )
        engine.dispose()
