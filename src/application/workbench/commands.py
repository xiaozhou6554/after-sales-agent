"""Employee-confirmed ticket commands kept separate from model-facing tools."""

from __future__ import annotations

import uuid
from typing import Protocol

from application.common.authorization import (
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import (
    OperationConfirmationUseCase,
    content_digest,
)
from application.common.ports.confirmation import ConfirmationConflictError
from domain.ticket.model import TicketStatus


class StaffTeamPort(Protocol):
    def belongs_to_any_team(
        self, tenant_id: str, subject_id: str, team_ids: tuple[str, ...]
    ) -> bool: ...


class WorkbenchTicketCommandPort(Protocol):
    def apply(
        self,
        ticket,
        *,
        auth: AuthContext,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
        idempotency_key: str,
    ): ...


class WorkbenchTicketReadPort(Protocol):
    def ticket_detail(self, auth: AuthContext, ticket_id: str) -> dict: ...


class WorkbenchTicketCommands:
    def __init__(
        self,
        reads: WorkbenchTicketReadPort,
        confirmations: OperationConfirmationUseCase,
        writes: WorkbenchTicketCommandPort,
        staff: StaffTeamPort,
    ) -> None:
        self._reads = reads
        self._confirmations = confirmations
        self._writes = writes
        self._staff = staff

    def _binding(
        self,
        auth: AuthContext,
        ticket_id: str,
        operation: str,
        content: dict,
        expected_revision: int,
    ) -> tuple[dict, OperationContext]:
        scope = "ticket:assign" if operation == "assign" else "ticket:status"
        verify_tool_context(auth, (scope,))
        detail = self._reads.ticket_detail(auth, ticket_id)
        binding = OperationContext.from_trusted_actor(
            auth,
            customer_id=detail["customer_id"],
            confirmer_subject_id=auth.subject_id,
            operation_type=operation,
            target_id=ticket_id,
            payload_digest=content_digest(content),
            expected_revision=expected_revision,
        )
        return detail, binding

    def prepare_assignment(
        self, auth: AuthContext, ticket_id: str, assigned_to: str, expected_revision: int
    ):
        detail, binding = self._binding(
            auth, ticket_id, "assign", {"assigned_to": assigned_to}, expected_revision
        )
        require_customer_access(auth, detail["customer_id"], tenant_id=auth.tenant_id)
        if not self._staff.belongs_to_any_team(
            auth.tenant_id, assigned_to, auth.data_scope.team_ids
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        return self._confirmations.prepare(binding)

    def prepare_status(
        self,
        auth: AuthContext,
        ticket_id: str,
        target_status: str,
        reason: str,
        expected_revision: int,
    ):
        detail, binding = self._binding(
            auth,
            ticket_id,
            "status",
            {"target_status": target_status, "reason": reason},
            expected_revision,
        )
        require_customer_access(auth, detail["customer_id"], tenant_id=auth.tenant_id)
        return self._confirmations.prepare(binding)

    def verify(self, auth: AuthContext, confirmation_id: str, challenge: str):
        record = self._confirmations.get(auth.tenant_id, confirmation_id)
        if (
            record is None
            or record.binding.actor_subject_id != auth.subject_id
            or record.binding.operation_type not in {"assign", "status"}
        ):
            raise ConfirmationConflictError("confirmation unavailable")
        scope = "ticket:assign" if record.binding.operation_type == "assign" else "ticket:status"
        verify_tool_context(auth, (scope,))
        require_customer_access(auth, record.binding.customer_id, tenant_id=auth.tenant_id)
        self._confirmations.verify(auth.tenant_id, confirmation_id, challenge)
        return confirmation_id

    def assign(
        self,
        auth: AuthContext,
        ticket_id: str,
        assigned_to: str,
        expected_revision: int,
        confirmation_id: str,
        idempotency_key: str,
    ):
        detail, binding = self._binding(
            auth, ticket_id, "assign", {"assigned_to": assigned_to}, expected_revision
        )
        require_customer_access(auth, detail["customer_id"], tenant_id=auth.tenant_id)
        if not self._staff.belongs_to_any_team(
            auth.tenant_id, assigned_to, auth.data_scope.team_ids
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        detail["assigned_to"] = assigned_to
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{auth.tenant_id}:{auth.subject_id}:assign:{idempotency_key}"
        ).hex
        result = self._writes.apply(
            detail,
            auth=auth,
            binding=binding,
            confirmation_id=confirmation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "ticket_id": ticket_id,
            "assigned_to": assigned_to,
            "revision": result.revision,
            "replayed": result.replayed,
        }

    def change_status(
        self,
        auth: AuthContext,
        ticket_id: str,
        target_status: str,
        reason: str,
        expected_revision: int,
        confirmation_id: str,
        idempotency_key: str,
    ):
        content = {"target_status": target_status, "reason": reason}
        detail, binding = self._binding(auth, ticket_id, "status", content, expected_revision)
        require_customer_access(auth, detail["customer_id"], tenant_id=auth.tenant_id)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{auth.tenant_id}:{auth.subject_id}:status:{idempotency_key}"
        ).hex
        detail["status"] = TicketStatus(detail["status"]).value
        detail["target_status"] = target_status
        detail["expected_revision"] = expected_revision
        detail["reason"] = reason
        result = self._writes.apply(
            detail,
            auth=auth,
            binding=binding,
            confirmation_id=confirmation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "ticket_id": ticket_id,
            "status": target_status,
            "revision": result.revision,
            "replayed": result.replayed,
        }
