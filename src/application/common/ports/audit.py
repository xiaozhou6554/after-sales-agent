"""审计仓储端口：事前持久化调用意图，事后追加结果（技术详细规格 20.3、25 章）。

- 审计记录先持久化，不能依赖尽力而为的异步消息；
- 事前审计无法持久化时，调用方必须拒绝高风险写入（AuditPersistenceError）；
- 载入本端口的摘要必须是脱敏后的内容（脱敏在审计用例统一执行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class AuditPersistenceError(RuntimeError):
    """审计意图/结果无法持久化；高风险写入必须因此拒绝。"""


@dataclass(frozen=True)
class AuditIntent:
    """工具执行前的调用意图记录（status 固定 pending）。"""

    audit_id: str
    tenant_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    thread_id: str | None
    actor_subject_id: str
    customer_id: str | None
    action: str
    resource_refs: tuple[str, ...] = field(default=())
    parameter_summary: dict = field(default_factory=dict)
    status: str = "pending"


@dataclass(frozen=True)
class AuditCompletion:
    """工具执行结束后的结果追加；同一 audit_id 只允许完成一次。"""

    audit_id: str
    tenant_id: str
    result_summary: dict = field(default_factory=dict)
    duration_ms: int | None = None
    status: str = "success"
    error_code: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class AuditAction:
    """单条终态审计（安全事件、业务动作等非工具调用场景）。"""

    audit_id: str
    tenant_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    thread_id: str | None
    actor_subject_id: str
    customer_id: str | None
    action: str
    status: str
    resource_refs: tuple[str, ...] = field(default=())
    parameter_summary: dict = field(default_factory=dict)
    result_summary: dict = field(default_factory=dict)
    duration_ms: int | None = None
    error_code: str | None = None
    event_id: str | None = None


class AuditSink(Protocol):
    """持久化端：MySQL 审计仓储实现；失败一律抛 AuditPersistenceError。"""

    def record_intent(self, intent: AuditIntent) -> None: ...

    def append_result(self, completion: AuditCompletion) -> None: ...

    def record_action(self, action: AuditAction) -> None: ...
