"""MCP 请求/响应信封与工具注册模型（《技术详细规格》26.6）。

- MCPRequest 的 AuthContext 不在模型内：通过受保护传输单独携带，
  不进入模型可见参数或返回值；
- parameter_summary 是脱敏摘要对象，原始参数只经 arguments 按工具专用 Schema 传输；
- ToolRegistration 以 data/contracts/mcp-tools.json 为唯一源，服务端加载并校验，
  不允许模型修改。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from interface.http.schemas.common import SCHEMA_VERSION, ErrorModel, StrictModel


class MCPTraceModel(StrictModel):
    """信封内嵌的链路字段（与 26.5 TraceContext 同构）。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    thread_id: str | None = None


class MCPRequestModel(StrictModel):
    trace: MCPTraceModel
    agent_name: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: Literal["request"] = "request"
    error_code: None = None
    parameter_summary: dict[str, Any] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPResponseModel(StrictModel):
    trace: MCPTraceModel
    agent_name: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: Literal["success", "empty", "partial", "degraded", "error"]
    error_code: str | None = None
    parameter_summary: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorModel | None = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> "MCPResponseModel":
        if self.status == "error":
            if self.error is None or not self.error_code:
                raise ValueError("error 响应必须携带 error 对象与 error_code")
        elif self.status in ("success", "empty") and (self.error is not None or self.error_code):
            raise ValueError(f"{self.status} 响应不得携带 error/error_code")
        return self


class ToolRegistrationModel(StrictModel):
    """字段以 data/contracts/mcp-tools.json 实际清单为准（唯一源）。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    access: Literal["read", "write"]
    side_effect: Literal["none", "business"]
    auth_scopes: list[str] = Field(min_length=1)
    requires_confirmation: bool
    idempotency_required: bool
    input_schema: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    response_schema_ref: str | None = None


class MCPToolErrorModel(StrictModel):
    """data/contracts/mcp-response.schema.json $defs/error：不含堆栈与链路外泄。"""

    error_code: str = Field(min_length=1)
    message: str = Field(max_length=500)
    retryable: bool
    details: dict[str, Any] | None = None


class MCPToolResponseModel(StrictModel):
    """工具响应信封：与 data/contracts/mcp-response.schema.json 逐字段对齐。

    AuthContext 不在信封内（受保护传输单独携带）；AuthContext 数据也不得
    出现在 data/parameter_summary 中。
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: Literal["success", "empty", "partial", "degraded", "error"]
    error_code: str | None = None
    parameter_summary: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] | None = None
    error: MCPToolErrorModel | None = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> "MCPToolResponseModel":
        if self.status == "error":
            if self.error is None or not self.error_code:
                raise ValueError("error 响应必须携带 error 对象且顶层 error_code 一致")
            if self.error.error_code != self.error_code:
                raise ValueError("顶层 error_code 必须与 error.error_code 一致")
        else:
            if self.error is not None:
                raise ValueError("非 error 响应不得携带 error 对象")
        return self


def build_tool_response(
    *,
    request_id: str,
    correlation_id: str,
    trace_id: str,
    invocation_id: str,
    tool_name: str,
    status: str,
    parameter_summary: dict[str, Any],
    data: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str | None = None,
    retryable: bool | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造并通过信封模型校验后返回纯 dict（供 MCP structuredContent 直接返回）。"""
    error_payload = None
    if error_code is not None:
        error_payload = MCPToolErrorModel(
            error_code=error_code, message=message or "", retryable=bool(retryable)
        ).model_dump(mode="json", exclude_none=True)
        if details:
            error_payload["details"] = details
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "invocation_id": invocation_id,
        "tool_name": tool_name,
        "status": status,
        "error_code": error_code,
        "parameter_summary": parameter_summary,
        "data": data,
        "error": error_payload,
    }
    # data/error 为 Schema 必备键（可为 null），仅 error.details 的 None 需剥离
    # （$defs/error 的 details 只接受对象或缺省），故最终序列化后回填干净 error。
    validated = MCPToolResponseModel.model_validate(envelope).model_dump(mode="json")
    if error_payload is not None:
        validated["error"] = error_payload
    return validated
