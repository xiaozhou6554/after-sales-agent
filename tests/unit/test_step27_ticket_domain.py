"""步骤27：工单领域规则不依赖身份、确认或存储。"""

from dataclasses import replace

import pytest

from domain.ticket import (
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


def ticket() -> Ticket:
    return Ticket(
        tenant_id="tenant-a",
        ticket_id="ticket-1",
        customer_id="customer-1",
        created_by="subject-1",
        title="售后问题",
        description_masked="设备无法启动",
        priority=TicketPriority.NORMAL,
    )


def message(message_id: str = "message-1") -> TicketMessage:
    return TicketMessage(
        message_id=message_id,
        author_subject_id="subject-1",
        content_masked="已补充设备型号",
        visibility=TicketVisibility.CUSTOMER,
    )


def test_create_and_legal_updates_are_immutable_and_increment_revision() -> None:
    original = ticket()
    assigned = original.assign("agent-1", expected_revision=1)
    prioritized = assigned.change_priority(TicketPriority.HIGH, expected_revision=2)
    progressed = prioritized.change_status(TicketStatus.IN_PROGRESS, expected_revision=3)
    updated = progressed.add_message(message(), expected_revision=4)

    assert original.status is TicketStatus.OPEN
    assert original.assigned_to is None
    assert original.messages == ()
    assert [assigned.revision, prioritized.revision, progressed.revision, updated.revision] == [
        2,
        3,
        4,
        5,
    ]
    assert updated.assigned_to == "agent-1"
    assert updated.priority is TicketPriority.HIGH
    assert updated.messages == (message(),)


def test_allowed_status_chain_and_direct_close() -> None:
    in_progress = ticket().change_status(TicketStatus.IN_PROGRESS, expected_revision=1)
    resolved = in_progress.change_status(TicketStatus.RESOLVED, expected_revision=2)
    closed = resolved.change_status(TicketStatus.CLOSED, expected_revision=3)
    assert closed.status is TicketStatus.CLOSED
    assert closed.revision == 4
    directly_closed = ticket().change_status(TicketStatus.CLOSED, expected_revision=1)
    closed_from_progress = in_progress.change_status(TicketStatus.CLOSED, expected_revision=2)
    assert directly_closed.status is TicketStatus.CLOSED
    assert closed_from_progress.status is TicketStatus.CLOSED


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (TicketStatus.OPEN, TicketStatus.RESOLVED),
        (TicketStatus.IN_PROGRESS, TicketStatus.OPEN),
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
        (TicketStatus.CLOSED, TicketStatus.OPEN),
    ],
)
def test_illegal_status_transition_is_rejected(start: TicketStatus, target: TicketStatus) -> None:
    with pytest.raises(InvalidTicketState):
        replace(ticket(), status=start).change_status(target, expected_revision=1)


def test_closed_ticket_cannot_be_changed_or_reopened() -> None:
    closed = ticket().change_status(TicketStatus.CLOSED, expected_revision=1)
    with pytest.raises(InvalidTicketState):
        closed.change_priority(TicketPriority.URGENT, expected_revision=2)
    with pytest.raises(InvalidTicketState):
        closed.assign("agent-1", expected_revision=2)
    with pytest.raises(InvalidTicketState):
        closed.add_message(message(), expected_revision=2)
    with pytest.raises(InvalidTicketState):
        closed.change_status(TicketStatus.OPEN, expected_revision=2)


def test_resolved_ticket_can_receive_follow_up_but_cannot_be_reassigned() -> None:
    resolved = replace(ticket(), status=TicketStatus.RESOLVED)
    assert resolved.add_message(message(), expected_revision=1).revision == 2
    with pytest.raises(InvalidTicketState):
        resolved.assign("agent-1", expected_revision=1)


@pytest.mark.parametrize(
    "operation",
    [
        lambda item: item.change_priority(TicketPriority.HIGH, expected_revision=0),
        lambda item: item.assign("agent-1", expected_revision=0),
        lambda item: item.change_status(TicketStatus.CLOSED, expected_revision=0),
        lambda item: item.add_message(message(), expected_revision=0),
    ],
)
def test_stale_version_rejects_every_write(operation) -> None:
    with pytest.raises(TicketVersionConflict):
        operation(ticket())


def test_repeated_operations_do_not_increment_version() -> None:
    item = ticket()
    with pytest.raises(DuplicateTicketOperation):
        item.change_priority(TicketPriority.NORMAL, expected_revision=1)
    with pytest.raises(DuplicateTicketOperation):
        item.change_status(TicketStatus.OPEN, expected_revision=1)
    assigned = item.assign("agent-1", expected_revision=1)
    with pytest.raises(DuplicateTicketOperation):
        assigned.assign("agent-1", expected_revision=2)
    updated = item.add_message(message(), expected_revision=1)
    with pytest.raises(DuplicateTicketOperation):
        updated.add_message(message(), expected_revision=2)
    with pytest.raises(TicketVersionConflict):
        assigned.assign("agent-1", expected_revision=1)
    assert assigned.revision == updated.revision == 2


@pytest.mark.parametrize("bad_priority", ["critical", "high", None])
def test_unknown_priority_is_rejected(bad_priority) -> None:
    with pytest.raises(InvalidTicket):
        replace(ticket(), priority=bad_priority)
    with pytest.raises(InvalidTicket):
        ticket().change_priority(bad_priority, expected_revision=1)


def test_invalid_state_version_and_message_are_rejected() -> None:
    with pytest.raises(InvalidTicket):
        replace(ticket(), status="pending")
    with pytest.raises(InvalidTicket):
        replace(ticket(), revision=0)
    with pytest.raises(InvalidTicket):
        ticket().change_status("pending", expected_revision=1)
    with pytest.raises(InvalidTicket):
        ticket().assign("  ", expected_revision=1)
    with pytest.raises(InvalidTicket):
        replace(message(), visibility="public")
    with pytest.raises(InvalidTicket):
        replace(ticket(), messages=(message(), message()))


def test_internal_message_visibility_is_preserved_not_authorized_by_domain() -> None:
    internal = replace(message(), visibility=TicketVisibility.INTERNAL)
    updated = ticket().add_message(internal, expected_revision=1)
    assert updated.messages[0].visibility is TicketVisibility.INTERNAL
