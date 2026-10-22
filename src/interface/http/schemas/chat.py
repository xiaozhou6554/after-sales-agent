"""对话请求与统一 API 响应契约（《技术详细规格》26.6）。

不变量（构造即校验，fail-closed）：
- status=pending 必须携带已持久化 async_task_id 且 next_action=wait；
- status=needs_input 必须给出 requested_inputs 且 next_action=ask_user；
- status=error 必须携带 error 对象；success/pending 的 error 必须为 null；
- handoff_required=true 不表示已成功转接，真实状态以业务记录为准。

身份与链路字段不出现在请求体：客户端不能覆盖可信身份、租户、链路或资源归属。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from interface.http.schemas.common import (
    SCHEMA_VERSION,
    BusinessRefModel,
    ErrorModel,
    EvidenceModel,
    RequestedInputModel,
    StrictModel,
)

# 输入上下文预算：空串拒绝；超长拒绝并要求重述，不无声截断敏感操作内容。
CHAT_MESSAGE_MAX_CHARS = 4000


class ChatRequestModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    message: str = Field(min_length=1, max_length=CHAT_MESSAGE_MAX_CHARS)
    thread_id: str | None = None
    # 客服代客时的目标客户；普通用户不传。授权仍由服务端按 AuthContext 校验。
    customer_id: str | None = None
    business_refs: list[BusinessRefModel] = Field(default_factory=list)


class APIResponseModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    thread_id: str | None = None
    status: Literal["success", "needs_input", "pending", "partial", "error"]
    answer: str
    next_action: Literal["finish", "ask_user", "handoff", "wait"]
    handoff_required: bool = False
    requested_inputs: list[RequestedInputModel] = Field(default_factory=list)
    sources: list[EvidenceModel] = Field(default_factory=list)
    async_task_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorModel | None = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> "APIResponseModel":
        if self.status == "pending":
            if not self.async_task_id:
                raise ValueError("pending 必须携带已持久化的 async_task_id")
            if self.next_action != "wait":
                raise ValueError("pending 的 next_action 必须为 wait")
        if self.status == "needs_input":
            if not self.requested_inputs:
                raise ValueError("needs_input 必须给出 requested_inputs")
            if self.next_action != "ask_user":
                raise ValueError("needs_input 的 next_action 必须为 ask_user")
        if self.status == "error" and self.error is None:
            raise ValueError("error 状态必须携带 error 对象")
        if self.status in ("success", "pending") and self.error is not None:
            raise ValueError(f"{self.status} 状态的 error 必须为 null")
        return self
