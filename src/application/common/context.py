"""通用身份与上下文契约（《技术详细规格》26.5，纯 Python，供用例与领域使用）。

- TraceContext：链路字段由可信服务端生成或验证，不从客户端输入恢复；
- AuthContext：只来自认证/授权系统，是服务到服务的受保护上下文，
  不放进模型可见参数或返回值；
- BusinessRef / RequestedInput：传输层同构模型的内部 DTO 形态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class TraceContext:
    schema_version: str
    request_id: str
    correlation_id: str
    trace_id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class DataScope:
    """客服代客范围：没有客户范围不默认拥有全部租户数据。"""

    customer_ids: tuple[str, ...] = ()
    team_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    subject_id: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    data_scope: DataScope
    session_id: str
    issued_at: datetime
    expires_at: datetime
    verification_ref: str | None = None

    def is_valid_at(self, moment: datetime) -> bool:
        """完整性（字段非空）由构造方保证；此处校验有效期与调用时间。"""
        if self.expires_at.tzinfo is None:
            expires = self.expires_at.replace(tzinfo=timezone.utc)
        else:
            expires = self.expires_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment <= expires


@dataclass(frozen=True)
class OperationContext:
    """服务端绑定的操作身份和内容；不得从模型参数直接构造。"""

    tenant_id: str
    actor_subject_id: str
    customer_id: str
    confirmer_subject_id: str
    operation_type: str
    target_id: str
    payload_digest: str
    expected_revision: int | None

    @classmethod
    def from_trusted_actor(
        cls,
        auth: AuthContext,
        *,
        customer_id: str,
        confirmer_subject_id: str,
        operation_type: str,
        target_id: str,
        payload_digest: str,
        expected_revision: int | None,
    ) -> "OperationContext":
        return cls(
            tenant_id=auth.tenant_id,
            actor_subject_id=auth.subject_id,
            customer_id=customer_id,
            confirmer_subject_id=confirmer_subject_id,
            operation_type=operation_type,
            target_id=target_id,
            payload_digest=payload_digest,
            expected_revision=expected_revision,
        )


@dataclass(frozen=True)
class BusinessRef:
    resource_type: str
    resource_id: str
    version: str | None = None


@dataclass(frozen=True)
class RequestedInput:
    field: str
    prompt: str
    input_type: str  # text / choice / secure_form
    required: bool
    form_ref: str | None = None


@dataclass(frozen=True)
class FieldError:
    field: str
    code: str


@dataclass(frozen=True)
class ErrorDto:
    """错误契约的应用层形态；details 只含字段路径与错误码。"""

    error_code: str
    message: str
    retryable: bool
    request_id: str
    correlation_id: str
    trace_id: str
    schema_version: str = "1.0"
    details: tuple[FieldError, ...] = field(default_factory=tuple)
