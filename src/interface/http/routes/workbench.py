"""受控员工工作台查询和人工接单入口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field

from application.common.authorization import AuthorizationDenied, verify_tool_context
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.ticket.handoff import HandoffConflict
from interface.http.middleware.auth import get_auth_context
from interface.http.routes.warehouse import _respond
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench", tags=["employee-workbench"])


class AcceptHandoffModel(StrictModel):
    expected_revision: int = Field(ge=1)


class AssignmentConfirmationModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    assigned_to: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)


class AssignmentCommitModel(StrictModel):
    assigned_to: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    confirmation_id: str = Field(min_length=1, max_length=64)


class StatusConfirmationModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    target_status: Literal["open", "in_progress", "resolved", "closed"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)


class StatusCommitModel(StrictModel):
    target_status: Literal["open", "in_progress", "resolved", "closed"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)
    confirmation_id: str = Field(min_length=1, max_length=64)


class WorkbenchVerifyModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    challenge: str = Field(min_length=1, max_length=128)


def _queries(request: Request):
    service = getattr(request.app.state, "workbench_queries", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "工作台查询服务暂不可用")
    return service


def _commands(request: Request):
    service = getattr(request.app.state, "workbench_ticket_commands", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "工单操作服务暂不可用")
    return service


@router.get("/customers")
def list_customers(
    request: Request,
    query: str = Query(default="", max_length=64),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
):
    return _respond(200, _queries(request).customers(auth, query=query, cursor=cursor, limit=limit))


@router.get("/tickets")
def list_tickets(
    request: Request,
    customer_id: str | None = Query(default=None, max_length=64),
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None,
    priority: Literal["low", "normal", "high", "urgent"] | None = None,
    assigned_to: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
):
    data = _queries(request).tickets(
        auth,
        customer_id=customer_id,
        status=status,
        priority=priority,
        assigned_to=assigned_to,
        cursor=cursor,
        limit=limit,
    )
    return _respond(200, data)


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)):
    return _respond(200, _queries(request).ticket_detail(auth, ticket_id))


@router.post("/tickets/{ticket_id}/assignment/confirmations", status_code=201)
def prepare_assignment(
    ticket_id: str,
    payload: AssignmentConfirmationModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        prepared = _commands(request).prepare_assignment(
            auth, ticket_id, payload.assigned_to, payload.expected_revision
        )
    except ValueError as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "工单当前版本或状态已变化") from exc
    return _respond(
        201,
        {
            "confirmation_id": prepared.record.confirmation_id,
            "expires_at": prepared.record.expires_at.isoformat(),
            "challenge": prepared.mock_challenge,
        },
        "分派内容已绑定，请明确确认",
    )


@router.post("/tickets/{ticket_id}/status/confirmations", status_code=201)
def prepare_status(
    ticket_id: str,
    payload: StatusConfirmationModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        prepared = _commands(request).prepare_status(
            auth, ticket_id, payload.target_status, payload.reason, payload.expected_revision
        )
    except ValueError as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "工单当前版本或状态已变化") from exc
    return _respond(
        201,
        {
            "confirmation_id": prepared.record.confirmation_id,
            "expires_at": prepared.record.expires_at.isoformat(),
            "challenge": prepared.mock_challenge,
        },
        "状态变更内容已绑定，请明确确认",
    )


@router.post("/ticket-confirmations/{confirmation_id}/verify")
def verify_ticket_confirmation(
    confirmation_id: str,
    payload: WorkbenchVerifyModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        _commands(request).verify(auth, confirmation_id, payload.challenge)
    except AuthorizationDenied:
        raise
    except Exception as exc:
        raise AuthorizationDenied(ErrorCode.CONFIRMATION_INVALID, "确认无效或已过期") from exc
    return _respond(
        200, {"confirmation_id": confirmation_id, "status": "verified"}, "员工确认已验证"
    )


@router.post("/tickets/{ticket_id}/assignment")
def assign_ticket(
    ticket_id: str,
    payload: AssignmentCommitModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        data = _commands(request).assign(
            auth,
            ticket_id,
            payload.assigned_to,
            payload.expected_revision,
            payload.confirmation_id,
            idempotency_key,
        )
    except AuthorizationDenied:
        raise
    except Exception as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "工单分派冲突，请刷新后重试") from exc
    return _respond(200, data, "工单已分派")


@router.post("/tickets/{ticket_id}/status")
def change_ticket_status(
    ticket_id: str,
    payload: StatusCommitModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        data = _commands(request).change_status(
            auth,
            ticket_id,
            payload.target_status,
            payload.reason,
            payload.expected_revision,
            payload.confirmation_id,
            idempotency_key,
        )
    except AuthorizationDenied:
        raise
    except Exception as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "工单状态冲突，请刷新后重试") from exc
    return _respond(200, data, "工单状态已更新")


@router.get("/handoffs")
def list_handoffs(
    request: Request,
    status: Literal["queued", "accepted", "unavailable"] | None = None,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
):
    return _respond(
        200, _queries(request).handoffs(auth, status=status, cursor=cursor, limit=limit)
    )


@router.post("/handoffs/{handoff_id}/accept")
def accept_handoff(
    handoff_id: str,
    payload: AcceptHandoffModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    verify_tool_context(auth, ("handoff:accept",))
    handoff = getattr(request.app.state, "human_handoff", None)
    if handoff is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "接单服务暂不可用")
    try:
        result = handoff.claim(
            auth,
            handoff_id=handoff_id,
            expected_revision=payload.expected_revision,
            idempotency_key=idempotency_key,
        )
    except HandoffConflict as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "队列项目已被接单或版本已变化") from exc
    return _respond(
        200,
        {
            "handoff_id": result.handoff_id,
            "status": result.status,
            "revision": result.revision,
            "ticket_id": result.ticket_id,
            "assigned_to": result.assigned_to,
            "replayed": result.replayed,
        },
        "已接收该人工请求",
    )


@router.get("/refund-requests")
def list_refund_requests(
    request: Request,
    status: Literal["reviewing", "approved", "rejected"] | None = None,
    customer_id: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
):
    data = _queries(request).refunds(
        auth,
        status=status,
        customer_id=customer_id,
        cursor=cursor,
        limit=limit,
    )
    return _respond(200, data)
