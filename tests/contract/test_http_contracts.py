"""HTTP 契约测试：五态样例、未知字段/缺字段/未知版本拒绝、身份不可覆盖。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from application.common.context import AuthContext, DataScope, RequestedInput, TraceContext
from application.common.dto import ApiResponseDto, ContractViolation
from interface.http.schemas.chat import APIResponseModel, ChatRequestModel
from interface.http.schemas.common import TraceContextModel
from interface.http.schemas.converters import (
    api_response_dto_to_model,
    chat_request_to_dto,
    trace_model_to_dto,
)
from tests.contract.samples import (
    API_RESPONSE_ASYNC_ACCEPTED,
    API_RESPONSE_EMPTY_SOURCES,
    API_RESPONSE_ERROR,
    API_RESPONSE_NEEDS_INPUT,
    API_RESPONSE_PARTIAL,
    API_RESPONSE_SUCCESS,
    TRACE_FIELDS,
)


def _as_chat_payload(**overrides):
    payload = {"schema_version": "1.0", "message": "帮我查一下退款进度", "business_refs": []}
    payload.update(overrides)
    return payload


# ---------- 合法样例 ----------


@pytest.mark.parametrize(
    "sample",
    [
        API_RESPONSE_SUCCESS,
        API_RESPONSE_EMPTY_SOURCES,
        API_RESPONSE_NEEDS_INPUT,
        API_RESPONSE_PARTIAL,
        API_RESPONSE_ASYNC_ACCEPTED,
        API_RESPONSE_ERROR,
    ],
    ids=["success", "empty", "needs_input", "partial", "async_accepted", "error"],
)
def test_fixed_samples_validate(sample: dict) -> None:
    model = APIResponseModel.model_validate(copy.deepcopy(sample))
    assert model.schema_version == "1.0"


def test_chat_request_validates_and_converts() -> None:
    request = ChatRequestModel.model_validate(_as_chat_payload())
    trace = TraceContext(
        schema_version="1.0",
        request_id=TRACE_FIELDS["request_id"],
        correlation_id=TRACE_FIELDS["correlation_id"],
        trace_id=TRACE_FIELDS["trace_id"],
    )
    dto = chat_request_to_dto(request, trace)
    assert dto.message == "帮我查一下退款进度"
    assert dto.customer_id is None and dto.thread_id is None


# ---------- 未知字段、缺字段、未知版本、非法状态 ----------


def test_unknown_field_rejected() -> None:
    sample = copy.deepcopy(API_RESPONSE_SUCCESS)
    sample["roles"] = ["admin"]  # 客户端试图携带身份字段
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_chat_request_cannot_override_identity_or_trace() -> None:
    # 请求体里任何身份/链路/权限字段都是未知字段，直接拒绝。
    for key, value in [
        ("tenant_id", "tenant-south"),
        ("subject_id", "admin-01"),
        ("roles", ["supervisor"]),
        ("scopes", ["order:read"]),
        ("trace_id", "trace-forged"),
        ("request_id", "req-forged"),
    ]:
        with pytest.raises(ValidationError):
            ChatRequestModel.model_validate(_as_chat_payload(**{key: value}))


def test_missing_required_field_rejected() -> None:
    sample = copy.deepcopy(API_RESPONSE_SUCCESS)
    del sample["answer"]
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_unknown_schema_version_rejected() -> None:
    sample = copy.deepcopy(API_RESPONSE_SUCCESS)
    sample["schema_version"] = "9.9"
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)

    with pytest.raises(ValidationError):
        ChatRequestModel.model_validate(_as_chat_payload(schema_version="9.9"))


def test_illegal_status_and_next_action_rejected() -> None:
    sample = copy.deepcopy(API_RESPONSE_SUCCESS)
    sample["status"] = "maybe"
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)
    sample["status"] = "success"
    sample["next_action"] = "retry_forever"
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_empty_message_rejected_and_overlong_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatRequestModel.model_validate(_as_chat_payload(message=""))
    with pytest.raises(ValidationError):
        ChatRequestModel.model_validate(_as_chat_payload(message="超" * 4001))


# ---------- 响应不变量（DTO 与传输模型两侧一致） ----------


def test_pending_without_task_id_rejected() -> None:
    sample = copy.deepcopy(API_RESPONSE_ASYNC_ACCEPTED)
    sample["async_task_id"] = None
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)
    with pytest.raises(ContractViolation, match="async_task_id"):
        ApiResponseDto(
            trace=TraceContext("1.0", "r", "c", "t"),
            status="pending",
            answer="受理中",
            next_action="wait",
        )


def test_pending_requires_wait_next_action() -> None:
    sample = copy.deepcopy(API_RESPONSE_ASYNC_ACCEPTED)
    sample["next_action"] = "finish"
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_needs_input_requires_requested_inputs() -> None:
    sample = copy.deepcopy(API_RESPONSE_NEEDS_INPUT)
    sample["requested_inputs"] = []
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_error_status_requires_error_object_and_success_forbids_it() -> None:
    sample = copy.deepcopy(API_RESPONSE_ERROR)
    sample["error"] = None
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)

    sample = copy.deepcopy(API_RESPONSE_SUCCESS)
    sample["error"] = {"error_code": "X", "message": "m", "retryable": False}
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(sample)


def test_error_details_carry_only_field_errors() -> None:
    sample = copy.deepcopy(API_RESPONSE_ERROR)
    sample["error"]["details"] = {"field_errors": [{"field": "order_id", "code": "missing"}]}
    APIResponseModel.model_validate(sample)

    bad = copy.deepcopy(API_RESPONSE_ERROR)
    bad["error"]["details"] = {
        "field_errors": [{"field": "order_id", "code": "missing", "stack": "Traceback..."}]
    }
    with pytest.raises(ValidationError):
        APIResponseModel.model_validate(bad)


# ---------- 受控转换：DTO → 传输模型 ----------


def test_api_response_dto_to_model_round_trip() -> None:
    dto = ApiResponseDto(
        trace=TraceContext(
            schema_version="1.0",
            request_id=TRACE_FIELDS["request_id"],
            correlation_id=TRACE_FIELDS["correlation_id"],
            trace_id=TRACE_FIELDS["trace_id"],
        ),
        status="needs_input",
        answer="请提供需要查询的订单号。",
        next_action="ask_user",
        requested_inputs=(RequestedInput("order_id", "请提供订单号", "text", True, None),),
    )
    model = api_response_dto_to_model(dto)
    assert model.status == "needs_input"
    assert model.next_action == "ask_user"
    assert model.requested_inputs[0].field == "order_id"


def test_trace_model_to_dto() -> None:
    model = TraceContextModel.model_validate(TRACE_FIELDS)
    dto = trace_model_to_dto(model)
    assert dto.request_id == TRACE_FIELDS["request_id"]
    assert dto.thread_id is None


# ---------- AuthContext 有效期 ----------


def test_auth_context_validity_window() -> None:
    auth = AuthContext(
        tenant_id="tenant-north",
        subject_id="agent-01",
        roles=("support",),
        scopes=("order:read",),
        data_scope=DataScope(customer_ids=("CUSTOMER-01",), team_ids=()),
        session_id="sess-1",
        issued_at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc),
    )
    assert auth.is_valid_at(datetime(2026, 10, 20, 8, 59, tzinfo=timezone.utc))
    assert not auth.is_valid_at(datetime(2026, 10, 20, 9, 0, 1, tzinfo=timezone.utc))
