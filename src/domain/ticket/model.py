"""工单聚合的状态、优先级和乐观版本规则。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class TicketDomainError(ValueError):
    """确定性的工单业务拒绝。"""


class InvalidTicket(TicketDomainError):
    """工单数据或目标值无效。"""


class InvalidTicketState(TicketDomainError):
    """当前状态禁止该操作或状态转换。"""


class TicketVersionConflict(TicketDomainError):
    """提交所依据的工单版本已过期。"""


class DuplicateTicketOperation(TicketDomainError):
    """同一当前版本上的重复操作不产生新版本。"""


class TicketPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketVisibility(str, Enum):
    CUSTOMER = "customer"
    INTERNAL = "internal"


_NEXT_STATUSES = {
    TicketStatus.OPEN: frozenset((TicketStatus.IN_PROGRESS, TicketStatus.CLOSED)),
    TicketStatus.IN_PROGRESS: frozenset((TicketStatus.RESOLVED, TicketStatus.CLOSED)),
    TicketStatus.RESOLVED: frozenset((TicketStatus.CLOSED,)),
    TicketStatus.CLOSED: frozenset(),
}


def _nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTicket(f"{name} must be nonblank")


def _enum(value: object, expected: type[Enum], name: str) -> None:
    if not isinstance(value, expected):
        raise InvalidTicket(f"{name} must be a {expected.__name__}")


@dataclass(frozen=True)
class TicketMessage:
    message_id: str
    author_subject_id: str
    content_masked: str
    visibility: TicketVisibility

    def __post_init__(self) -> None:
        _nonblank(self.message_id, "message_id")
        _nonblank(self.author_subject_id, "author_subject_id")
        _nonblank(self.content_masked, "content_masked")
        _enum(self.visibility, TicketVisibility, "visibility")


@dataclass(frozen=True)
class Ticket:
    tenant_id: str
    ticket_id: str
    customer_id: str
    created_by: str
    title: str
    description_masked: str
    priority: TicketPriority
    status: TicketStatus = TicketStatus.OPEN
    assigned_to: str | None = None
    revision: int = 1
    messages: tuple[TicketMessage, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "ticket_id",
            "customer_id",
            "created_by",
            "title",
            "description_masked",
        ):
            _nonblank(getattr(self, name), name)
        _enum(self.priority, TicketPriority, "priority")
        _enum(self.status, TicketStatus, "status")
        if self.assigned_to is not None:
            _nonblank(self.assigned_to, "assigned_to")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise InvalidTicket("revision must be a positive integer")
        if not isinstance(self.messages, tuple) or any(
            not isinstance(message, TicketMessage) for message in self.messages
        ):
            raise InvalidTicket("messages must be TicketMessage values")
        ids = [message.message_id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise InvalidTicket("message_id must be unique within a ticket")

    def _check_revision(self, expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise InvalidTicket("expected_revision must be an integer")
        if expected_revision != self.revision:
            raise TicketVersionConflict("ticket revision has changed")

    def _check_writable(self) -> None:
        if self.status is TicketStatus.CLOSED:
            raise InvalidTicketState("closed ticket cannot be changed")

    def change_priority(self, priority: TicketPriority, *, expected_revision: int) -> Ticket:
        self._check_revision(expected_revision)
        self._check_writable()
        _enum(priority, TicketPriority, "priority")
        if priority is self.priority:
            raise DuplicateTicketOperation("priority is already set")
        return replace(self, priority=priority, revision=self.revision + 1)

    def assign(self, subject_id: str, *, expected_revision: int) -> Ticket:
        self._check_revision(expected_revision)
        if self.status not in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS):
            raise InvalidTicketState("only active tickets can be assigned")
        _nonblank(subject_id, "assigned_to")
        if subject_id == self.assigned_to:
            raise DuplicateTicketOperation("ticket is already assigned to this subject")
        return replace(self, assigned_to=subject_id, revision=self.revision + 1)

    def change_status(self, target: TicketStatus, *, expected_revision: int) -> Ticket:
        self._check_revision(expected_revision)
        _enum(target, TicketStatus, "status")
        if target is self.status:
            raise DuplicateTicketOperation("ticket is already in this status")
        if target not in _NEXT_STATUSES[self.status]:
            raise InvalidTicketState("ticket status transition is not allowed")
        return replace(self, status=target, revision=self.revision + 1)

    def add_message(self, message: TicketMessage, *, expected_revision: int) -> Ticket:
        self._check_revision(expected_revision)
        self._check_writable()
        if not isinstance(message, TicketMessage):
            raise InvalidTicket("message must be a TicketMessage")
        if any(existing.message_id == message.message_id for existing in self.messages):
            raise DuplicateTicketOperation("message_id already exists")
        return replace(self, messages=(*self.messages, message), revision=self.revision + 1)
