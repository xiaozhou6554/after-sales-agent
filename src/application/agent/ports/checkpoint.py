"""Checkpoint 仓储端口（依赖倒置，技术详细规格 169/1039 行）。

LangGraph 适配器 ``MySQLCheckpointSaver`` 位于基础设施层并实现本端口；应用层
（对话服务/任务服务）只依赖本端口做恢复核对与运维清理，不接触 SQL 客户端。

恢复键为联合键（tenant_id, thread_id, checkpoint_ns），不得仅凭 thread_id
访问他人/他租户数据（技术详细规格 545 行）；行内 State/元数据/pending_writes
均为脱敏后内容（543 行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CheckpointSummary:
    """最新 Checkpoint 概要：恢复前核对已完成调用与工具执行记录的依据。"""

    checkpoint_id: str
    parent_checkpoint_id: str | None
    current_node: str | None
    state_schema_version: str
    revision: int
    saved_at: str
    # 脱敏后的 State 快照（tool_calls/observations/step_count 等可核对字段）。
    state: dict[str, Any] = field(default_factory=dict)
    # 脱敏后的未提交节点写入：[{task_id, task_path, channel, value}]。
    pending_writes: list[dict[str, Any]] = field(default_factory=list)


class CheckpointRepositoryPort(Protocol):
    def by_id(
        self, tenant_id: str, thread_id: str, checkpoint_id: str, *, checkpoint_ns: str = ""
    ) -> CheckpointSummary | None:
        """按完整恢复键读取指定版本，供任务结果和原暂停点核对。"""
        ...

    def latest(
        self, tenant_id: str, thread_id: str, *, checkpoint_ns: str = ""
    ) -> CheckpointSummary | None:
        """读取线程最新 Checkpoint 概要；无线程历史返回 None。"""
        ...

    def delete_checkpoints(self, tenant_id: str, thread_id: str) -> None:
        """按租户删除整线程 Checkpoint 数据（含 pending_writes）。

        不与 LangGraph 基类 ``delete_thread(thread_id)`` 同名：删除必须
        携带租户，避免跨租户模糊删除。
        """
        ...


class CheckpointPersistenceError(RuntimeError):
    """Checkpoint 读写失败；按依赖失败处理，不得伪造恢复成功。"""


class CheckpointUnavailableError(CheckpointPersistenceError):
    """存储传输层不可用（连接/超时）；可重试的依赖失败。"""


class CheckpointConflictError(CheckpointPersistenceError):
    """版本/并发冲突（分叉、父缺失、同 ID 不同内容）；结构化返回，不静默覆盖。"""


class CheckpointSchemaError(CheckpointPersistenceError):
    """state_schema_version 不兼容；阻止恢复并转人工，不得丢弃未知 State。"""
