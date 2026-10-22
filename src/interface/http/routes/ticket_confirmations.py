"""受控工单确认表单（步骤24，技术详细规格 27.7）。

该入口只签发绑定了可信身份、目标、规范化内容和版本的确认记录。
它不执行工单写入，也不接受客户端提交的摘要、角色或客户归属；后续
工单命令必须在同一绑定上完成一次性验证和条件消费。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, OperationContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import content_digest
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(tags=["ticket-confirmations"])


class TicketContentModel(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10000)
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    assigned_to: str | None = Field(default=None, min_length=1, max_length=128)
    target_status: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _must_have_content(self) -> "TicketContentModel":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("确认内容不能为空")
        return self


class TicketConfirmationRequestModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    operation_type: Literal["create", "update", "assign", "status"]
    target_id: str | None = Field(default=None, min_length=1, max_length=64)
    content: TicketContentModel
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_target_and_revision(self) -> "TicketConfirmationRequestModel":
        if self.operation_type != "create" and not self.target_id:
            raise ValueError("更新、分派和状态变更必须绑定目标工单")
        if self.operation_type == "create" and self.expected_revision is not None:
            raise ValueError("创建工单不能携带预期版本")
        if self.operation_type != "create" and self.expected_revision is None:
            raise ValueError("更新、分派和状态变更必须携带预期版本")
        return self


def _single_customer(auth: AuthContext) -> str:
    customer_ids = auth.data_scope.customer_ids
    if len(customer_ids) != 1 or customer_ids[0] == "*":
        raise AuthorizationDenied(
            ErrorCode.INVALID_ARGUMENT,
            "确认表单必须绑定一个明确的客户范围",
        )
    return customer_ids[0]


@router.post("/ticket-confirmations", status_code=201)
def create_ticket_confirmation(
    payload: TicketConfirmationRequestModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    use_case = getattr(request.app.state, "confirmation_use_case", None)
    if use_case is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认服务暂不可用")

    customer_id = _single_customer(auth)
    normalized_content = payload.content.model_dump(exclude_none=True)
    binding = OperationContext.from_trusted_actor(
        auth,
        customer_id=customer_id,
        confirmer_subject_id=auth.subject_id,
        operation_type=payload.operation_type,
        target_id=payload.target_id or "new",
        payload_digest=content_digest(normalized_content),
        expected_revision=payload.expected_revision,
    )
    try:
        prepared = use_case.prepare(binding)
    except AuthorizationDenied:
        raise
    except Exception as exc:  # noqa: BLE001 - 外部存储错误不泄露细节
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认服务暂不可用") from exc

    trace = request_trace()
    response = APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status="success",
        answer="确认内容已保存，请在有效期内完成明确确认。",
        next_action="finish",
        data={
            "confirmation": {
                "confirmation_id": prepared.record.confirmation_id,
                "expires_at": prepared.record.expires_at.isoformat(),
                "content_digest": binding.payload_digest,
            }
        },
    )
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))
