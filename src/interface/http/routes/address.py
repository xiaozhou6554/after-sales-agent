"""仅本地开发启用的受控收货地址表单；明文地址不经模型工具。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.common.ports.confirmation import ConfirmationConflictError
from application.order.use_cases.address_change import AddressChangeDenied
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(tags=["address"])


class AddressFormModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    receiver_name: str = Field(min_length=1, max_length=64)
    contact_phone: str = Field(min_length=1, max_length=32)
    province: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=32)
    district: str = Field(min_length=1, max_length=32)
    detail_address: str = Field(min_length=1, max_length=128)


class AddressVerifyModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    challenge: str = Field(min_length=1, max_length=128)


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
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "地址修改服务暂不可用")
    return service


def _change_data(result: dict) -> dict:
    observed = result["observed_at"]
    data = {
        "address_change_id": result["address_change_id"],
        "order_id": result["order_id"],
        "status": result["status"],
        "expected_order_version": result["expected_order_version"],
        "result_order_version": result["result_order_version"],
        "observed_at": observed.isoformat() if hasattr(observed, "isoformat") else str(observed),
    }
    return data


@router.post("/orders/{order_id}/shipping-address-changes", status_code=201)
def create_address_change(
    order_id: str,
    payload: AddressFormModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    service = _required(request, "address_change")
    try:
        fields = payload.model_dump(exclude={"schema_version"})
        prepared = service.prepare_from_fields(auth, order_id, fields)
    except AddressChangeDenied as exc:
        return _respond(
            200,
            "当前订单暂不能修改收货地址。",
            {
                "eligible": False,
                "order_id": order_id,
                "denial_reasons": exc.reasons,
            },
            status="partial",
        )
    except ValueError as exc:
        raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
    except ConfirmationConflictError as exc:
        raise AuthorizationDenied(ErrorCode.CONFIRMATION_INVALID, "确认无效或已过期") from exc
    record = prepared.record
    return _respond(
        201,
        "地址已登记为待确认修改；请完成本地模拟一次性验证后再提交。",
        {
            "address_change_id": record.address_change_id,
            "order_id": record.order_id,
            "status": record.status,
            "expected_order_version": record.expected_order_version,
            "masked_summary": record.address.masked_summary(),
            "confirmation_id": prepared.confirmation.record.confirmation_id,
            "expires_at": prepared.confirmation.record.expires_at.isoformat(),
            "mock_challenge": prepared.confirmation.mock_challenge,
            "synthetic_only": True,
        },
    )


@router.post("/shipping-address-confirmations/{confirmation_id}/verify")
def verify_address_confirmation(
    confirmation_id: str,
    payload: AddressVerifyModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    service = _required(request, "address_change")
    try:
        record = service.verify(auth, confirmation_id, payload.challenge)
    except ConfirmationConflictError as exc:
        raise AuthorizationDenied(ErrorCode.CONFIRMATION_INVALID, "确认无效或已过期") from exc
    except AuthorizationDenied:
        raise
    return _respond(
        200,
        "一次性验证已完成，仍需明确提交修改。",
        {
            "confirmation_id": record.confirmation_id,
            "status": record.status,
            "expires_at": record.expires_at.isoformat(),
        },
    )


@router.get("/shipping-address-changes/{address_change_id}")
def query_address_change(
    address_change_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    service = _required(request, "address_change")
    try:
        data = _change_data(service.query(auth, address_change_id))
    except ValueError as exc:
        raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
    return _respond(200, "以下为已核实的地址修改状态。", data)


@router.get("/shipping-address-changes/by-confirmation/{confirmation_id}")
def recover_address_change(
    confirmation_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    service = _required(request, "address_change")
    data = _change_data(service.by_confirmation(auth, confirmation_id))
    return _respond(200, "以下为已核实的地址修改状态。", data)
