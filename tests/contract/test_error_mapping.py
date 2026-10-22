"""错误映射契约测试：错误码→HTTP 状态全覆盖、安全语义不泄露内部细节。"""

from __future__ import annotations

import pytest

from application.common.errors import ERROR_HTTP_STATUS, ErrorCode, http_status_for
from interface.http.schemas.common import ErrorModel


def test_every_error_code_has_http_mapping() -> None:
    assert {code.value for code in ErrorCode} == {code.value for code in ERROR_HTTP_STATUS}
    assert len(ERROR_HTTP_STATUS) == len(ErrorCode)


def test_specified_status_semantics() -> None:
    assert http_status_for("UNAUTHENTICATED") == 401
    assert http_status_for("RESOURCE_UNAVAILABLE") == 404  # 未授权与不存在统一 404
    assert http_status_for("INVALID_ARGUMENT") == 400
    assert http_status_for("SCHEMA_VERSION_UNSUPPORTED") == 400
    assert http_status_for("VERSION_CONFLICT") == 409
    assert http_status_for("IDEMPOTENCY_CONFLICT") == 409
    assert http_status_for("CONFIRMATION_INVALID") == 409
    assert http_status_for("STATE_CONFLICT") == 409
    assert http_status_for("DEPENDENCY_UNAVAILABLE") == 503
    assert http_status_for("TIMEOUT") == 504
    assert http_status_for("RATE_LIMITED") == 429


def test_unknown_error_code_maps_to_400() -> None:
    assert http_status_for("SOMETHING_ELSE") == 400


def test_error_model_rejects_stack_trace_and_credentials() -> None:
    base = dict(
        error_code="DEPENDENCY_UNAVAILABLE",
        message="依赖服务暂时不可用，请稍后重试。",
        retryable=True,
        request_id="r",
        correlation_id="c",
        trace_id="t",
    )
    ErrorModel.model_validate(base)

    with pytest.raises(Exception):
        ErrorModel.model_validate({**base, "stack_trace": "Traceback (most recent call last)..."})
    with pytest.raises(Exception):
        ErrorModel.model_validate({**base, "credentials": {"api_key": "sk-xxx"}})


def test_resource_unavailable_message_is_generic() -> None:
    # 未授权与不存在统一语义：消息不区分资源是否存在，不泄露内部异常。
    error = ErrorModel.model_validate(
        dict(
            error_code="RESOURCE_UNAVAILABLE",
            message="资源不可用。",
            retryable=False,
            request_id="r",
            correlation_id="c",
            trace_id="t",
            details={"field_errors": []},
        )
    )
    assert "不存在" not in error.message
    assert "OperationalError" not in error.message
    assert error.details == {"field_errors": []}
