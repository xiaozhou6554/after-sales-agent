"""Server-owned bridge between verified HTTP ticket writes and model-visible arguments.

The model may supply only the fields in ``visible_*``.  Confirmation and
idempotency material stays in the returned intent, never in tool input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from application.common.context import AuthContext, TraceContext
from application.ticket.service import TicketContextSnapshot
from domain.ticket import TicketPriority, TicketVisibility


class TicketToolIntentConflict(ValueError):
    """No unique, currently authorized ticket write intent is available."""


class TicketToolIntentPort(Protocol):
    def resolve_create(
        self,
        auth: AuthContext,
        *,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
    ) -> TicketToolIntent: ...

    def resolve_update(
        self,
        auth: AuthContext,
        *,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
    ) -> TicketToolIntent: ...


def visible_digest(fields: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def visible_create(
    customer_id: str, title: str, description_masked: str, priority: TicketPriority
) -> dict[str, str]:
    if not isinstance(priority, TicketPriority):
        raise TicketToolIntentConflict("invalid ticket priority")
    return dict(
        customer_id=customer_id,
        title=title,
        description_masked=description_masked,
        priority=priority.value,
    )


def visible_update(
    ticket_id: str, content_masked: str, visibility: TicketVisibility
) -> dict[str, str]:
    if not isinstance(visibility, TicketVisibility):
        raise TicketToolIntentConflict("invalid ticket visibility")
    return dict(ticket_id=ticket_id, content_masked=content_masked, visibility=visibility.value)


@dataclass(frozen=True)
class TicketToolIntent:
    operation_type: str
    confirmation_id: str
    idempotency_key: str
    expected_revision: int | None
    customer_id: str
    ticket_id: str
    context: TicketContextSnapshot
    trace: TraceContext
    visible_fields: dict[str, str]
    handoff_id: str | None = None

    def commit_kwargs(self) -> dict[str, Any]:
        """Pass with trusted AuthContext to TicketApplicationService.create/update."""
        if self.operation_type == "ticket:create":
            return dict(
                customer_id=self.customer_id,
                title=self.visible_fields["title"],
                description_masked=self.visible_fields["description_masked"],
                priority=TicketPriority(self.visible_fields["priority"]),
                context=self.context,
                trace=self.trace,
                idempotency_key=self.idempotency_key,
                confirmation_id=self.confirmation_id,
                handoff_id=self.handoff_id,
            )
        if self.operation_type == "ticket:update":
            return dict(
                ticket_id=self.ticket_id,
                content_masked=self.visible_fields["content_masked"],
                visibility=TicketVisibility(self.visible_fields["visibility"]),
                expected_revision=self.expected_revision,
                context=self.context,
                trace=self.trace,
                idempotency_key=self.idempotency_key,
                confirmation_id=self.confirmation_id,
            )
        raise TicketToolIntentConflict("unsupported ticket operation")
