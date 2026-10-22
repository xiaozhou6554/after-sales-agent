"""Development-only workbench payment surface: refund execution and reconciliation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.payment_errors import PaymentConflict
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench", tags=["payment-refunds"])


class AllocationModel(StrictModel):
    request_line_id: str = Field(min_length=1, max_length=64)
    component: Literal["goods", "shipping"]
    amount_minor: int = Field(ge=1, le=100_000_000)


class SubmitExecutionModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    refund_request_id: str = Field(min_length=1, max_length=64)
    allocations: list[AllocationModel] = Field(min_length=1, max_length=50)

    def line_dicts(self) -> list[dict]:
        return [
            {
                "request_line_id": item.request_line_id,
                "component": item.component,
                "amount_minor": item.amount_minor,
            }
            for item in self.allocations
        ]


class ReconciliationRunModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    merchant_id: str = Field(min_length=1, max_length=64)
    currency: Literal["CNY"]
    window_start: datetime
    window_end: datetime


def _service(request: Request):
    service = getattr(request.app.state, "refund_execution", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


def _failure(exc: Exception):
    if isinstance(exc, AuthorizationDenied):
        raise exc
    raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "退款执行冲突或不可用") from exc


def _trace() -> TraceContext:
    trace = request_trace()
    return TraceContext(
        "1.0", trace.request_id, trace.correlation_id, trace.trace_id, trace.thread_id
    )


def _respond(code: int, data: dict, answer: str) -> JSONResponse:
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


@router.post("/refund-executions")
def submit_execution(
    payload: SubmitExecutionModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        result = _service(request).submit(
            auth, payload.refund_request_id, payload.line_dicts(),
            idempotency_key=idempotency_key, trace=_trace(),
        )
    except PaymentConflict as exc:
        _failure(exc)
    return _respond(
        200 if result.get("replayed") else 201, result, "退款执行已受理"
    )


@router.post("/refund-executions/{execution_id}/recover")
def recover_execution(
    execution_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    try:
        result = _service(request).recover(auth, execution_id, trace=_trace())
    except PaymentConflict as exc:
        _failure(exc)
    return _respond(200, result, "退款执行已核实")


@router.post("/refund-callbacks")
def refund_callback(
    request: Request,
    payload: dict,
    x_channel_signature: str = Header(alias="X-Channel-Signature", min_length=32),
):
    try:
        result = _service(request).handle_callback(
            payload, x_channel_signature, trace=_trace()
        )
    except PaymentConflict as exc:
        _failure(exc)
    return _respond(200, result, "回调已处理")


@router.post("/refund-reconciliation/runs")
def run_reconciliation(
    payload: ReconciliationRunModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        result = _service(request).reconcile(
            auth,
            merchant_id=payload.merchant_id,
            currency=payload.currency,
            window_start=payload.window_start,
            window_end=payload.window_end,
            trace=_trace(),
        )
    except PaymentConflict as exc:
        _failure(exc)
    return _respond(
        200 if result.get("replayed") else 201, result, "对账批次已执行"
    )
