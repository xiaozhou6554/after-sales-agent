"""Development-only controlled ticket and Mock handoff HTTP commands."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.idempotency import IdempotencyConflictError
from application.ticket.handoff import HandoffConflict
from application.ticket.service import (
    InvalidTicket,
    InvalidTicketState,
    TicketContextSnapshot,
    TicketPriority,
    TicketVersionConflict,
    TicketVisibility,
)
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/dev", tags=["dev-ticket-commands"])


class ContextModel(StrictModel):
    question_masked: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=1000)
    agent_results: list[str] = Field(default_factory=list, max_length=20)
    tool_result_summaries: list[str] = Field(default_factory=list, max_length=20)
    failure_reason: str | None = Field(default=None, max_length=512)
    business_refs: dict[str, str] = Field(default_factory=dict)
    current_state: str = Field(min_length=1, max_length=64)
    next_action: str = Field(min_length=1, max_length=512)
    source_message_ids: list[str] = Field(default_factory=list, max_length=20)

    def snapshot(self) -> TicketContextSnapshot:
        return TicketContextSnapshot(
            self.question_masked,
            self.summary,
            tuple(self.agent_results),
            tuple(self.tool_result_summaries),
            self.failure_reason,
            tuple(self.business_refs.items()),
            self.current_state,
            self.next_action,
            tuple(self.source_message_ids),
        )


class CreateModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    customer_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    description_masked: str = Field(min_length=1, max_length=4000)
    priority: TicketPriority
    context: ContextModel


class UpdateModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    content_masked: str = Field(min_length=1, max_length=4000)
    visibility: TicketVisibility
    expected_revision: int = Field(ge=1)
    context: ContextModel


class CommitCreateModel(CreateModel):
    operation_ref: str = Field(min_length=32, max_length=32)
    confirmation_id: str = Field(min_length=1, max_length=64)


class CommitUpdateModel(UpdateModel):
    operation_ref: str = Field(min_length=32, max_length=32)
    confirmation_id: str = Field(min_length=1, max_length=64)


class VerifyModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    challenge: str = Field(min_length=1, max_length=128)


class HandoffRequestModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    customer_id: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(min_length=1, max_length=64)
    context: ContextModel
    ticket_id: str | None = Field(default=None, min_length=1, max_length=64)


class HandoffClaimModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    expected_revision: int = Field(ge=1)
    ticket_id: str | None = Field(default=None, min_length=1, max_length=64)


def _respond(code: int, data: dict, answer: str = "操作已完成") -> JSONResponse:
    trace = request_trace()
    body = APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status="success",
        answer=answer,
        next_action="finish",
        data=data,
    )
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


def _required(request: Request, name: str):
    service = getattr(request.app.state, name, None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


def _key(auth: AuthContext, kind: str, ref: str) -> str:
    try:
        uuid.UUID(hex=ref)
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "操作引用无效") from exc
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{auth.tenant_id}:{auth.subject_id}:{kind}:{ref}").hex


def _trace(ref: str) -> TraceContext:
    # Stable server-derived values are required by the create confirmation digest.
    return TraceContext("1.0", f"req-{ref}", f"corr-{ref}", f"trace-{ref}", f"thread-{ref}")


def _fallback_ref(auth: AuthContext, handoff_id: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{auth.tenant_id}:{auth.subject_id}:handoff:fallback:{handoff_id}",
    ).hex


def _handoff_request_ref(auth: AuthContext, key: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{auth.tenant_id}:{auth.subject_id}:handoff:request:{key}"
    ).hex


def _unavailable_handoff(
    request: Request,
    auth: AuthContext,
    handoff_id: str,
    customer_id: str,
    handoff_request_key: str,
    *,
    allow_linked: bool = False,
):
    # The fallback is a customer confirmation, even when staff initiated the handoff.
    verify_tool_context(auth, ("ticket:create",))
    require_customer_access(auth, customer_id, tenant_id=auth.tenant_id)
    if "customer" not in auth.roles:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    expected_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{auth.tenant_id}\x00{auth.subject_id}\x00handoff\x00"
        f"{_handoff_request_ref(auth, handoff_request_key)}",
    ).hex
    if handoff_id != expected_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    lookup = _required(request, "handoff_fallback_lookup")
    record = lookup(auth.tenant_id, handoff_id)
    if record is None or record[0] != customer_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    handoff = record[1]
    if handoff.status != "unavailable" or (handoff.ticket_id is not None and not allow_linked):
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "转接并非无人工可用且无关联工单")
    return handoff


def _fallback_failure(exc: Exception):
    try:
        _failure(exc)
    except AuthorizationDenied as denied:
        if denied.error_code == ErrorCode.DEPENDENCY_UNAVAILABLE:
            raise AuthorizationDenied(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "工单结果未确认；请用相同转接记录和确认凭证重试，暂无法提供工单编号",
            ) from exc
        raise


def _register_intent(request: Request, auth: AuthContext, method: str, **kwargs) -> None:
    """Persist the server-verified write intent so MCP writes can resolve it.

    Registration must happen after the confirmation is verified but before the
    commit consumes it; failures here abort the commit (fail-closed).
    """
    store = getattr(request.app.state, "ticket_tool_intent", None)
    if store is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    getattr(store, method)(auth, **kwargs)


def _failure(exc: Exception):
    if isinstance(exc, AuthorizationDenied):
        raise exc
    if isinstance(exc, (ConfirmationConflictError,)):
        code = ErrorCode.CONFIRMATION_INVALID
    elif isinstance(exc, IdempotencyConflictError):
        code = ErrorCode.IDEMPOTENCY_CONFLICT
    elif isinstance(exc, TicketVersionConflict):
        code = ErrorCode.VERSION_CONFLICT
    elif isinstance(exc, (HandoffConflict, InvalidTicketState)):
        code = ErrorCode.STATE_CONFLICT
    elif isinstance(exc, (ValueError, InvalidTicket)):
        code = ErrorCode.INVALID_ARGUMENT
    else:
        code = ErrorCode.DEPENDENCY_UNAVAILABLE
    raise AuthorizationDenied(
        code, "操作冲突或不可用" if code != ErrorCode.DEPENDENCY_UNAVAILABLE else "服务暂不可用"
    ) from exc


@router.post("/tickets/prepare-create")
def prepare_create(
    payload: CreateModel, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    ref = uuid.uuid4().hex
    try:
        prepared = _required(request, "ticket_commands").prepare_create(
            auth,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(ref),
            idempotency_key=_key(auth, "ticket:create", ref),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        201,
        {
            "operation_ref": ref,
            "ticket_id": prepared.ticket_id,
            "confirmation_id": prepared.confirmation.record.confirmation_id,
            "expires_at": prepared.confirmation.record.expires_at.isoformat(),
            "challenge": prepared.confirmation.mock_challenge,
        },
        "请明确确认工单创建",
    )


@router.post("/handoffs/{handoff_id}/fallback/prepare")
def prepare_handoff_fallback(
    handoff_id: str,
    payload: CreateModel,
    request: Request,
    handoff_request_key: str = Header(
        alias="Handoff-Request-Key",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    ref = _fallback_ref(auth, handoff_id)
    try:
        _unavailable_handoff(request, auth, handoff_id, payload.customer_id, handoff_request_key)
        prepared = _required(request, "ticket_commands").prepare_create(
            auth,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(ref),
            idempotency_key=_key(auth, "ticket:create", ref),
            handoff_id=handoff_id,
        )
    except Exception as exc:
        _fallback_failure(exc)
    return _respond(
        201,
        {
            "handoff_id": handoff_id,
            "operation_ref": ref,
            "confirmation_id": prepared.confirmation.record.confirmation_id,
            "expires_at": prepared.confirmation.record.expires_at.isoformat(),
            "challenge": prepared.confirmation.mock_challenge,
            "operation": "confirmation_prepared",
        },
        "人工暂不可用；本次仅准备内容确认，提交结果以提交或重放响应为准",
    )


@router.post("/handoffs/{handoff_id}/fallback/commit")
def commit_handoff_fallback(
    handoff_id: str,
    payload: CommitCreateModel,
    request: Request,
    handoff_request_key: str = Header(
        alias="Handoff-Request-Key",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    ref = _fallback_ref(auth, handoff_id)
    try:
        if payload.operation_ref != ref:
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "操作引用与转接记录不匹配")
        handoff = _unavailable_handoff(
            request,
            auth,
            handoff_id,
            payload.customer_id,
            handoff_request_key,
            allow_linked=True,
        )
        service = _required(request, "ticket_commands")
        _register_intent(
            request,
            auth,
            "register_create",
            confirmation_id=payload.confirmation_id,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(ref),
            idempotency_key=_key(auth, "ticket:create", ref),
            handoff_id=handoff_id,
        )
        result = service.create(
            auth,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(ref),
            idempotency_key=_key(auth, "ticket:create", ref),
            confirmation_id=payload.confirmation_id,
            handoff_id=handoff_id,
        )
        if handoff.ticket_id is not None and handoff.ticket_id != result.ticket_id:
            raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "转接工单关联与原操作不一致")
    except Exception as exc:
        _fallback_failure(exc)
    return _respond(
        200 if result.replayed else 201,
        {"handoff_id": handoff_id, **result.__dict__},
        "工单已创建" if not result.replayed else "工单创建结果已确认",
    )


@router.post("/tickets/{ticket_id}/prepare-update")
def prepare_update(
    ticket_id: str,
    payload: UpdateModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    ref = uuid.uuid4().hex
    try:
        prepared = _required(request, "ticket_commands").prepare_update(
            auth,
            ticket_id=ticket_id,
            content_masked=payload.content_masked,
            visibility=payload.visibility,
            expected_revision=payload.expected_revision,
            context=payload.context.snapshot(),
            idempotency_key=_key(auth, "ticket:update", ref),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        201,
        {
            "operation_ref": ref,
            "ticket_id": prepared.ticket_id,
            "confirmation_id": prepared.confirmation.record.confirmation_id,
            "expires_at": prepared.confirmation.record.expires_at.isoformat(),
            "challenge": prepared.confirmation.mock_challenge,
        },
        "请明确确认工单更新",
    )


@router.post("/ticket-confirmations/{confirmation_id}/verify")
def verify(
    confirmation_id: str,
    payload: VerifyModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        _required(request, "ticket_commands")
        flow = _required(request, "confirmation_use_case")
        confirmation = flow.get(auth.tenant_id, confirmation_id)
        if confirmation is None:
            raise ConfirmationConflictError("confirmation unavailable")
        if (
            confirmation.binding.actor_subject_id != auth.subject_id
            or confirmation.binding.operation_type not in {"ticket:create", "ticket:update"}
        ):
            raise ConfirmationConflictError("confirmation unavailable")
        confirmation = flow.verify(auth.tenant_id, confirmation_id, payload.challenge)
    except Exception as exc:
        _failure(exc)
    return _respond(200, {"confirmation_id": confirmation_id, "status": confirmation.status})


@router.post("/tickets/create")
def create(
    payload: CommitCreateModel, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    try:
        _register_intent(
            request,
            auth,
            "register_create",
            confirmation_id=payload.confirmation_id,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(payload.operation_ref),
            idempotency_key=_key(auth, "ticket:create", payload.operation_ref),
        )
        result = _required(request, "ticket_commands").create(
            auth,
            customer_id=payload.customer_id,
            title=payload.title,
            description_masked=payload.description_masked,
            priority=payload.priority,
            context=payload.context.snapshot(),
            trace=_trace(payload.operation_ref),
            idempotency_key=_key(auth, "ticket:create", payload.operation_ref),
            confirmation_id=payload.confirmation_id,
        )
    except Exception as exc:
        _failure(exc)
    return _respond(200 if result.replayed else 201, result.__dict__)


@router.post("/tickets/{ticket_id}/update")
def update(
    ticket_id: str,
    payload: CommitUpdateModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        _register_intent(
            request,
            auth,
            "register_update",
            confirmation_id=payload.confirmation_id,
            ticket_id=ticket_id,
            content_masked=payload.content_masked,
            visibility=payload.visibility,
            expected_revision=payload.expected_revision,
            context=payload.context.snapshot(),
            trace=_trace(payload.operation_ref),
            idempotency_key=_key(auth, "ticket:update", payload.operation_ref),
        )
        result = _required(request, "ticket_commands").update(
            auth,
            ticket_id=ticket_id,
            content_masked=payload.content_masked,
            visibility=payload.visibility,
            expected_revision=payload.expected_revision,
            context=payload.context.snapshot(),
            idempotency_key=_key(auth, "ticket:update", payload.operation_ref),
            confirmation_id=payload.confirmation_id,
            trace=_trace(payload.operation_ref),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(200, result.__dict__)


@router.post("/handoffs/request")
def request_handoff(
    payload: HandoffRequestModel,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    auth: AuthContext = Depends(get_auth_context),
):
    trace = request_trace()
    ref = _handoff_request_ref(auth, idempotency_key)
    handoff_trace = TraceContext(
        "1.0", trace.request_id, trace.correlation_id, trace.trace_id, f"thread-{ref}"
    )
    try:
        result = _required(request, "human_handoff").request(
            auth,
            handoff_trace,
            customer_id=payload.customer_id,
            reason_code=payload.reason_code,
            context=payload.context.snapshot(),
            idempotency_key=ref,
            available=getattr(request.app.state, "human_handoff_available", True),
            ticket_id=payload.ticket_id,
        )
    except Exception as exc:
        _failure(exc)
    if result.status == "unavailable":
        return _respond(
            201,
            result.__dict__,
            "人工暂不可用；如需工单处理，请核对内容并明确确认创建工单",
        )
    return _respond(201, result.__dict__)


@router.post("/handoffs/{handoff_id}/claim")
def claim_handoff(
    handoff_id: str,
    payload: HandoffClaimModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        result = _required(request, "human_handoff").claim(
            auth,
            handoff_id=handoff_id,
            expected_revision=payload.expected_revision,
            idempotency_key=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{auth.tenant_id}:{auth.subject_id}:handoff:claim:{handoff_id}:{payload.expected_revision}",
            ).hex,
            ticket_id=payload.ticket_id,
        )
    except Exception as exc:
        _failure(exc)
    return _respond(200, result.__dict__)
