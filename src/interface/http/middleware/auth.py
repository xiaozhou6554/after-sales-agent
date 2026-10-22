"""认证中间件（《技术详细规格》16.1、26.6、26.8）。

- Gateway 依据服务端登录态签发不可变 AuthContext；请求体不携带身份字段；
- 401 仅用于未认证/会话失效；未授权与不存在统一 404 + RESOURCE_UNAVAILABLE；
- 链路三元组由服务端生成（步骤08 扩展为正式日志/链路记录），响应信封
  一律使用 26.6 APIResponse 结构。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from application.common.authorization import AuthorizationDenied
from application.common.context import FieldError, TraceContext
from application.common.dto import ErrorDto
from application.common.errors import ErrorCode
from application.common.tracing import (
    SubjectFrame,
    bind_request_context,
    current_subject,
    current_trace,
)
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import ErrorModel
from interface.http.schemas.converters import error_dto_to_model

BEARER_PREFIX = "bearer "

security_logger = logging.getLogger("after_sales.security")


def new_server_trace(thread_id: str | None = None) -> TraceContext:
    """服务端生成链路三元组；客户端伪造的链路字段不进入响应。"""
    return TraceContext(
        schema_version="1.0",
        request_id=f"req-{uuid.uuid4().hex}",
        correlation_id=f"corr-{uuid.uuid4().hex}",
        trace_id=f"trace-{uuid.uuid4().hex}",
        thread_id=thread_id,
    )


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != BEARER_PREFIX.strip() or not token:
        return None
    return token.strip()


def get_auth_context(request: Request):
    """FastAPI 依赖：解析 Bearer 会话令牌并还原可信 AuthContext。"""
    gateway = request.app.state.auth
    token = bearer_token(request)
    if token is None:
        raise AuthorizationDenied(ErrorCode.UNAUTHENTICATED, "缺少会话凭证")
    auth = gateway.resolve(token)
    # 真实主体挂到共享 scope：BaseHTTPMiddleware 的独立任务上下文读不到
    # 路由侧的 contextvar 变更，scope 是中间件与路由之间可靠的共享通道。
    request.state.subject_frame = SubjectFrame(auth.tenant_id, auth.subject_id)
    trace = current_trace()
    if trace is not None:
        bind_request_context(trace, request.state.subject_frame)
    return auth


def error_envelope(trace: TraceContext, error: ErrorDto) -> APIResponseModel:
    model_error: ErrorModel = error_dto_to_model(error)
    return APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status="error",
        answer="当前无法处理该请求。",
        next_action="finish",
        error=model_error,
    )


def denied_response(trace: TraceContext, denied: AuthorizationDenied) -> JSONResponse:
    # 安全日志：越权拒绝必须留痕（16.1/736 行），只记录码与路径级信息，不含令牌。
    subject = current_subject()
    security_logger.warning(
        "authorization denied: %s",
        denied.error_code.value,
        extra={
            "details": {"message": denied.message},
            "request_id": trace.request_id,
            "correlation_id": trace.correlation_id,
            "trace_id": trace.trace_id,
            "thread_id": trace.thread_id,
            "tenant_id": subject.tenant_id if subject else None,
            "subject_id": subject.subject_id if subject else None,
        },
    )
    error = ErrorDto(
        error_code=denied.error_code.value,
        message=denied.message,
        retryable=False,
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
    )
    payload = error_envelope(trace, error).model_dump(mode="json")
    return JSONResponse(status_code=denied.http_status, content=payload)


def request_trace() -> TraceContext:
    """复用当前请求已绑定的链路，保证一次请求一个 trace；未绑定才新生成。"""
    return current_trace() or new_server_trace()


def install_error_handlers(app: FastAPI) -> None:
    """把公共错误统一转为 26.5/26.6 信封：格式错误 400，授权拒绝按错误码映射。"""

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> Response:
        trace = request_trace()
        field_errors = tuple(
            FieldError(
                field=".".join(str(part) for part in err.get("loc", ())),
                code=str(err.get("type", "invalid")),
            )
            for err in exc.errors()[:10]
        )
        error = ErrorDto(
            error_code=ErrorCode.INVALID_ARGUMENT.value,
            message="请求格式错误",
            retryable=False,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
            details=field_errors,
        )
        payload = error_envelope(trace, error).model_dump(mode="json")
        return JSONResponse(status_code=400, content=payload)

    @app.exception_handler(AuthorizationDenied)
    async def _handle_denied(request: Request, exc: AuthorizationDenied) -> Response:
        return denied_response(request_trace(), exc)
