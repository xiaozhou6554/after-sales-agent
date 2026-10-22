"""The ticket MCP surface is deliberately a public, read-only summary."""

from datetime import datetime, timezone
from types import SimpleNamespace

from domain.ticket import Ticket, TicketMessage, TicketPriority, TicketVisibility
from infrastructure.mcp.server_factory import build_ticket_read_tool_handlers
from interface.mcp.tools import _ticket_data


def test_ticket_projection_never_returns_messages_or_structured_context() -> None:
    record = SimpleNamespace(
        ticket=Ticket(
            "tenant-north",
            "TICKET-1",
            "CUSTOMER-01",
            "customer-north-01",
            "public title",
            "description-marker",
            TicketPriority.NORMAL,
            messages=(
                TicketMessage(
                    "MESSAGE-1", "support-north-01", "internal-marker", TicketVisibility.INTERNAL
                ),
            ),
        ),
        context_snapshot={"summary": "context-marker"},
        updated_at=datetime(2026, 10, 21, 1, 2, 3, tzinfo=timezone.utc),
    )
    data = _ticket_data(record)
    assert data == {
        "found": True,
        "ticket": {
            "ticket_id": "TICKET-1",
            "status": "open",
            "priority": "normal",
            "revision": 1,
            "assigned_to": None,
            "updated_at": "2026-10-21T01:02:03+00:00",
        },
    }


def test_ticket_handler_registers_only_query() -> None:
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "success"}

    deps = object()
    handlers = build_ticket_read_tool_handlers(deps, execute)
    assert set(handlers) == {"query_ticket"}
    assert handlers["query_ticket"]("TICKET-1") == {"status": "success"}
    assert calls[0][0] == ("query_ticket", {"ticket_id": "TICKET-1"})
    assert calls[0][1]["deps"] is deps
