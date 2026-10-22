"""会话记忆端口（依赖倒置）：步骤11 起 Redis 为主仓储，内存仅作显式降级。

只存经过脱敏的对话可见文本与工具调用摘要（名称/状态/错误码），不存工具
参数原文、AuthContext 或令牌；租户隔离由复合键（tenant_id, thread_id）与
主体归属校验共同强制。每次读写成功后按既定 TTL 刷新（技术详细规格 11.2）。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from application.agent.ports.llm import ChatMessage


class SessionRepositoryPort(Protocol):
    def append_turn_once(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        task_id: str,
        question: str,
        answer: str,
        *,
        tool_summaries: Sequence[dict] = (),
    ) -> bool:
        """原子追加后台对话轮；相同任务已写入时返回 False。"""
        ...

    def append(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        message: ChatMessage,
        *,
        tool_summaries: Sequence[dict] = (),
    ) -> None:
        """追加一条对话消息；键不存在时创建，主体不匹配时拒绝写入。"""
        ...

    def load(self, tenant_id: str, thread_id: str, *, subject_id: str) -> list[ChatMessage]:
        """按序读取既有对话；无历史或主体不匹配（他人会话）返回空列表。"""
        ...


class MemoryPersistenceError(RuntimeError):
    """记忆读写失败；按依赖失败处理，不得静默丢弃对话上下文。"""


class MemoryUnavailableError(MemoryPersistenceError):
    """记忆仓储传输层不可用（连接/超时）；显式降级开关仅吸收此类失败。"""
