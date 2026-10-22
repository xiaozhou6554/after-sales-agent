"""仅本地开发启用的受控退款表单；写入不经模型工具。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.idempotency import IdempotencyConflictError
from application.refund.qualification import RefundLineSelection
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(tags=["refund"])


class RefundLineModel(StrictModel):
    order_line_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1)


class RefundQuoteRequestModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    reason_code: str = Field(min_length=1, max_length=64)
    lines: list[RefundLineModel] = Field(min_length=1)


class RefundVerifyModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    challenge: str = Field(min_length=1, max_length=128)


class RefundSubmitModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    quote_id: str = Field(min_length=1, max_length=64)
    confirmation_id: str = Field(min_length=1, max_length=64)


class RefundReviewModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    decision: Literal["approved", "rejected"]
    expected_revision: int = Field(ge=1)
    review_reason: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")


def _respond(status_code: int, answer: str, data: dict, *, status="success") -> JSONResponse:
    trace = request_trace()
    response = APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status=status,
        answer=answer,
        next_action="finish",
        data=data,
    )
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


def _required(request: Request, name: str):
    service = getattr(request.app.state, name, None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "退款服务暂不可用")
    return service


def _server_idempotency_key(auth: AuthContext, confirmation_id: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{auth.tenant_id}:{auth.subject_id}:refund_request:{confirmation_id}",
    ).hex


@router.post("/orders/{order_id}/refund-quotes", status_code=201)
def create_refund_quote(
    order_id: str,
    payload: RefundQuoteRequestModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    qualification = _required(request, "refund_qualification")
    assessment, quote = qualification.calculate_quote(
        auth,
        order_id,
        payload.reason_code,
        tuple(RefundLineSelection(line.order_line_id, line.quantity) for line in payload.lines),
    )
    if quote is None:
        return _respond(
            200,
            "暂不能自动报价，请核实退款条件或联系人工。",
            {
                "eligibility": assessment.eligibility,
                "can_submit": False,
                "reason_code": assessment.reason_code,
                "missing_information": list(assessment.missing_fields),
            },
            status="partial",
        )
    return _respond(
        201,
        "报价已生成，尚未提交退款申请。",
        {
            "quote_id": quote.quote_id,
            "order_id": quote.order_id,
            "amount_minor": quote.amount_minor,
            "goods_minor": quote.goods_minor,
            "shipping_minor": quote.shipping_minor,
            "currency": quote.currency,
            "reason_code": quote.reason_code,
            "eligibility": quote.eligibility,
            "policy_version": quote.policy_version,
            "order_version": quote.order_version,
            "payment_version": quote.payment_version,
            "expires_at": quote.expires_at.isoformat(),
            "lines": [line.__dict__ for line in quote.lines],
        },
    )


@router.post("/orders/{order_id}/refund-quotes/{quote_id}/confirm", status_code=201)
def prepare_refund_confirmation(
    order_id: str,
    quote_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    submissions = _required(request, "refund_submission")
    quote = submissions.authorized_quote(auth, quote_id)
    if quote.order_id != order_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
    flow = _required(request, "refund_confirmation")
    prepared = flow.prepare(auth, quote_id)
    tool_intent = getattr(request.app.state, "refund_tool_intent", None)
    if tool_intent is not None:
        tool_intent.register(auth, quote, prepared.record.confirmation_id)
    return _respond(
        201,
        "请核对报价并完成本地模拟一次性验证。",
        {
            "confirmation_id": prepared.record.confirmation_id,
            "expires_at": prepared.record.expires_at.isoformat(),
            "mock_challenge": prepared.mock_challenge,
            "synthetic_only": True,
        },
    )


@router.post("/refund-confirmations/{confirmation_id}/verify")
def verify_refund_confirmation(
    confirmation_id: str,
    payload: RefundVerifyModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    flow = _required(request, "refund_confirmation")
    try:
        record = flow.verify(auth, confirmation_id, payload.challenge)
    except ConfirmationConflictError as exc:
        raise AuthorizationDenied(ErrorCode.CONFIRMATION_INVALID, "确认无效或已过期") from exc
    return _respond(
        200,
        "一次性验证已完成，仍需明确提交申请。",
        {
            "confirmation_id": record.confirmation_id,
            "status": record.status,
            "expires_at": record.expires_at.isoformat(),
        },
    )


@router.post("/refund-requests", status_code=201)
def submit_refund_request(
    payload: RefundSubmitModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    service = _required(request, "refund_submission")
    try:
        result = service.submit(
            auth,
            payload.quote_id,
            payload.confirmation_id,
            _server_idempotency_key(auth, payload.confirmation_id),
            request_trace(),
        )
    except ConfirmationConflictError as exc:
        raise AuthorizationDenied(ErrorCode.CONFIRMATION_INVALID, "确认无效或已过期") from exc
    except (ValueError, IdempotencyConflictError) as exc:
        raise AuthorizationDenied(
            ErrorCode.STATE_CONFLICT, "退款申请状态已变化，请查询原结果"
        ) from exc
    return _respond(
        200 if result.replayed else 201,
        "申请已进入审核，资金尚未退回。",
        {
            "refund_request_id": result.refund_request_id,
            "review_status": result.status,
            "amount_minor": result.amount_minor,
            "currency": result.currency,
            "funds_status": "not_executed",
            "replayed": result.replayed,
        },
    )


@router.get("/refund-requests/by-confirmation/{confirmation_id}")
def recover_refund_request(
    confirmation_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    query = _required(request, "refund_query")
    snapshot = query.by_original_key(auth, _server_idempotency_key(auth, confirmation_id))
    return _refund_snapshot_response(snapshot)


@router.get("/refund-requests/{refund_request_id}")
def query_refund_request(
    refund_request_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    query = _required(request, "refund_query")
    return _refund_snapshot_response(query.by_id(auth, refund_request_id))


def _refund_snapshot_response(snapshot) -> JSONResponse:
    return _respond(
        200,
        "以下为已核实的申请状态；审核通过不表示资金已退。",
        {
            "refund_request_id": snapshot.refund_request_id,
            "order_id": snapshot.order_id,
            "review_status": snapshot.review_status,
            "return_status": snapshot.return_status,
            "funds_status": snapshot.funds_status,
            "amount_minor": snapshot.amount_minor,
            "currency": snapshot.currency,
            "revision": snapshot.revision,
            "updated_at": snapshot.updated_at.isoformat(),
        },
    )


@router.post("/workbench/refund-requests/{refund_request_id}/review")
def review_refund_request(
    refund_request_id: str,
    payload: RefundReviewModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    review = _required(request, "refund_reviewer")
    try:
        result = review.decide(
            auth,
            refund_request_id,
            expected_revision=payload.expected_revision,
            approve=payload.decision == "approved",
            reason_code=payload.review_reason,
            idempotency_key=idempotency_key,
            trace=request_trace(),
        )
    except ValueError as exc:
        raise AuthorizationDenied(
            ErrorCode.STATE_CONFLICT, "退款审核状态或版本已变化，请查询当前状态"
        ) from exc
    return _respond(
        200,
        "审核决定已记录；审核通过不表示资金已退。",
        {
            "refund_request_id": result.refund_request_id,
            "review_status": result.status,
            "revision": result.revision,
            "funds_status": "unknown",
        },
    )
