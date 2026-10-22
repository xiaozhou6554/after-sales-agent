"""认证路由（技术详细规格 27.2）。

- POST /auth/mock/session：MockAuthAdapter，仅开发/测试环境由 bootstrap 注册；
  只接受预置 account_id，客户端不能指定角色或 scope；
- GET /me：GetCurrentIdentity，返回真实主体、角色、scope 及授权范围摘要，
  不返回可由模型修改的身份对象。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from application.common.authorization import MockLoginDisabled, identity_summary
from application.common.context import AuthContext, TraceContext
from application.common.ports.auth import MockSessionGrant
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(tags=["auth"])


class MockSessionRequestModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    # 只允许选择后端预置账户；角色/scope/授权范围全部由服务端 fixture 决定。
    account_id: str = Field(min_length=1)


def _success(trace: TraceContext, answer: str, data: dict) -> JSONResponse:
    model = APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status="success",
        answer=answer,
        next_action="finish",
        data=data,
    )
    return JSONResponse(status_code=200, content=model.model_dump(mode="json"))


@router.post("/auth/mock/session")
def create_mock_session(payload: MockSessionRequestModel, request: Request) -> JSONResponse:
    gateway = request.app.state.auth
    if not gateway.mock_login_enabled():
        raise MockLoginDisabled("Mock 登录在当前环境已关闭")
    grant: MockSessionGrant = gateway.issue_mock_session(payload.account_id)
    trace = request_trace()
    auth = grant.auth
    return _success(
        trace,
        "Mock 登录成功。",
        {
            "session_token": grant.session_token,
            "token_type": "bearer",
            "expires_at": auth.expires_at.isoformat(),
            "tenant_id": auth.tenant_id,
            "subject_id": auth.subject_id,
            "roles": list(auth.roles),
        },
    )


@router.get("/me")
def get_current_identity(
    request: Request, auth: AuthContext = Depends(get_auth_context)
) -> JSONResponse:
    trace: TraceContext = request_trace()
    return _success(trace, "当前身份信息。", identity_summary(auth))
