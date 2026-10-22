"""公共错误码与 HTTP 状态映射（《技术详细规格》26.8）。

- 401 仅用于未认证；未授权资源与不存在统一 404 + RESOURCE_UNAVAILABLE；
- 格式错误 400；业务/版本/幂等/确认冲突 409；依赖失败 503；同步超时 504；
- 后台接受任务 202（由异步受理路径使用，不属于错误映射）。
- retryable 只表示是否允许依据操作类型和剩余预算恢复/查询，
  不授权自动重放高风险写操作。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    STATE_CONFLICT = "STATE_CONFLICT"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"


# 与 26.6 的 HTTP 语义一一对应；新增错误码必须先进入枚举再补映射。
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.SCHEMA_VERSION_UNSUPPORTED: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.RESOURCE_UNAVAILABLE: 404,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.CONFIRMATION_INVALID: 409,
    ErrorCode.STATE_CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
}


def http_status_for(error_code: str) -> int:
    """错误码到 HTTP 状态的受控映射；未知错误码按格式错误 400 处理。"""
    try:
        return ERROR_HTTP_STATUS[ErrorCode(error_code)]
    except ValueError:
        return 400
