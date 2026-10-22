"""工单领域的纯规则，不包含授权、确认或持久化。"""

from domain.ticket.model import (
    DuplicateTicketOperation,
    InvalidTicket,
    InvalidTicketState,
    Ticket,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    TicketVersionConflict,
    TicketVisibility,
)

__all__ = [
    "DuplicateTicketOperation",
    "InvalidTicket",
    "InvalidTicketState",
    "Ticket",
    "TicketMessage",
    "TicketPriority",
    "TicketStatus",
    "TicketVersionConflict",
    "TicketVisibility",
]
