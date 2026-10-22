"""Development-only workbench returns surface: WMS receipt and inspection."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench", tags=["warehouse-returns"])


class ReceiptLineModel(StrictModel):
    line_id: str = Field(min_length=1, max_length=64)
    received_qty: int = Field(ge=1, le=10000)


class InspectionLineModel(StrictModel):
    line_id: str = Field(min_length=1, max_length=64)
    accepted_qty: int = Field(ge=0, le=10000)
    rejected_qty: int = Field(ge=0, le=10000)
    reason_code: str | None = Field(default=None, min_length=1, max_length=64)


class ReceiptPrepareModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    lines: list[ReceiptLineModel] = Field(min_length=1, max_length=50)
    expected_revision: int = Field(ge=0)

    def line_dicts(self) -> list[dict]:
        return [{"line_id": item.line_id, "received_qty": item.received_qty}
                for item in self.lines]


class InspectionPrepareModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    lines: list[InspectionLineModel] = Field(min_length=1, max_length=50)
    expected_revision: int = Field(ge=0)

    def line_dicts(self) -> list[dict]:
        return [
            {
                "line_id": item.line_id,
                "accepted_qty": item.accepted_qty,
                "rejected_qty": item.rejected_qty,
                "reason_code": item.reason_code or "inspection_result",
            }
            for item in self.lines
        ]


class ReceiptCommitModel(ReceiptPrepareModel):
    operation_ref: str = Field(min_length=32, max_length=32)
    confirmation_id: str = Field(min_length=1, max_length=64)


class InspectionCommitModel(InspectionPrepareModel):
    operation_ref: str = Field(min_length=32, max_length=32)
    confirmation_id: str = Field(min_length=1, max_length=64)


class CreateReturnModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    refund_request_id: str = Field(min_length=1, max_length=64)


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


def _service(request: Request):
    service = getattr(request.app.state, "warehouse_service", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


def _failure(exc: Exception):
    if isinstance(exc, AuthorizationDenied):
        raise exc
    raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "退货操作冲突或不可用") from exc


def _trace() -> TraceContext:
    trace = request_trace()
    return TraceContext(
        "1.0", trace.request_id, trace.correlation_id, trace.trace_id, trace.thread_id
    )


@router.post("/returns", status_code=201)
def create_return(
    payload: CreateReturnModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        progress = _service(request).create_return(
            auth, payload.refund_request_id,
            idempotency_key=idempotency_key, trace=_trace(),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(201, progress.as_dict(), "退货单已登记")


@router.get("/returns/{return_id}")
def query_return(
    return_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    try:
        progress = _service(request).query_progress(auth, return_id)
    except Exception as exc:
        _failure(exc)
    return _respond(200, progress.as_dict())


@router.post("/returns/{return_id}/receipts/prepare", status_code=201)
def prepare_receipt(
    return_id: str,
    payload: ReceiptPrepareModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        prepared = _service(request).prepare_receipt(
            auth, return_id, payload.line_dicts(), payload.expected_revision
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        201, {**prepared, "operation": "confirmation_prepared"},
        "请核对收货明细并完成确认",
    )


@router.post("/returns/{return_id}/receipts")
def commit_receipt(
    return_id: str,
    payload: ReceiptCommitModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        result = _service(request).record_receipt(
            auth, return_id, payload.line_dicts(),
            expected_revision=payload.expected_revision,
            confirmation_id=payload.confirmation_id,
            idempotency_key=idempotency_key,
            trace=_trace(),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        200 if result.get("replayed") else 201, result, "收货已登记"
    )


@router.post("/returns/{return_id}/inspections/prepare", status_code=201)
def prepare_inspection(
    return_id: str,
    payload: InspectionPrepareModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        prepared = _service(request).prepare_inspection(
            auth, return_id, payload.line_dicts(), payload.expected_revision
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        201, {**prepared, "operation": "confirmation_prepared"},
        "请核对质检结果并完成确认",
    )


@router.post("/returns/{return_id}/inspections")
def commit_inspection(
    return_id: str,
    payload: InspectionCommitModel,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        result = _service(request).record_inspection(
            auth, return_id, payload.line_dicts(),
            expected_revision=payload.expected_revision,
            confirmation_id=payload.confirmation_id,
            idempotency_key=idempotency_key,
            trace=_trace(),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(
        200 if result.get("replayed") else 201, result, "质检已登记"
    )
