"""进程内会话仓储：仅作开发/测试的显式内存降级（生产禁止，15 章）。

与 RedisSessionRepository 保持同一端口语义：主体归属校验、脱敏写入、
容量截断；数据进程私有、重启即失，健康状态必须如实标注 degraded，
不得宣称跨重启持久化。
"""

from __future__ import annotations

from application.agent.ports.llm import ChatMessage
from application.agent.ports.memory import MemoryPersistenceError
from application.common.masking import mask_text
from application.memory.dto.session import SessionSnapshot, SummaryConflictError


class InMemorySessionRepository:
    """SessionRepositoryPort 的内存降级实现（memory_fallback 语义）。"""

    def __init__(self, *, max_messages: int = 20) -> None:
        self._store: dict[tuple[str, str], dict] = {}
        self._max_messages = max_messages

    def append(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        message: ChatMessage,
        *,
        tool_summaries: tuple[dict, ...] = (),
    ) -> None:
        record = self._store.get((tenant_id, thread_id))
        if record is not None and record["subject_id"] != subject_id:
            raise MemoryPersistenceError("会话主体不匹配，拒绝写入他人会话")
        if record is None:
            record = {
                "subject_id": subject_id,
                "messages": [],
                "tool_summaries": [],
                "message_offset": 0,
                "state_summary": {},
            }
            self._store[(tenant_id, thread_id)] = record
        record["messages"].append({"role": message.role, "content": mask_text(message.content)})
        record["tool_summaries"].append([dict(item) for item in tool_summaries])
        if len(record["messages"]) > self._max_messages:
            overflow = len(record["messages"]) - self._max_messages
            del record["messages"][:overflow]
            del record["tool_summaries"][:overflow]
            record["message_offset"] += overflow

    def append_turn_once(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        task_id: str,
        question: str,
        answer: str,
        *,
        tool_summaries=(),
    ) -> bool:
        record = self._store.get((tenant_id, thread_id))
        if record is not None and record["subject_id"] != subject_id:
            raise MemoryPersistenceError("会话主体不匹配，拒绝写入他人会话")
        if record is not None and task_id in record.get("completed_tasks", set()):
            return False
        self.append(tenant_id, thread_id, subject_id, ChatMessage(role="user", content=question))
        self.append(
            tenant_id,
            thread_id,
            subject_id,
            ChatMessage(role="assistant", content=answer),
            tool_summaries=tuple(tool_summaries),
        )
        self._store[(tenant_id, thread_id)].setdefault("completed_tasks", set()).add(task_id)
        return True

    def load(self, tenant_id: str, thread_id: str, *, subject_id: str) -> list[ChatMessage]:
        record = self._store.get((tenant_id, thread_id))
        if record is None or record["subject_id"] != subject_id:
            return []  # 他人会话视同无历史：跨主体不串上下文
        return [
            ChatMessage(role=item["role"], content=item["content"]) for item in record["messages"]
        ]

    def load_snapshot(self, tenant_id: str, thread_id: str, *, subject_id: str) -> SessionSnapshot:
        messages = self.load(tenant_id, thread_id, subject_id=subject_id)
        record = self._store.get((tenant_id, thread_id))
        if record is None or record["subject_id"] != subject_id:
            return SessionSnapshot((), 0, "", 0)
        summary = record["state_summary"]
        return SessionSnapshot(
            tuple(messages),
            record["message_offset"],
            str(summary.get("content", "")),
            int(summary.get("covered_until", 0)),
        )

    def save_summary(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        subject_id: str,
        expected_covered_until: int,
        covered_until: int,
        summary: str,
    ) -> None:
        record = self._store.get((tenant_id, thread_id))
        if record is None or record["subject_id"] != subject_id:
            raise MemoryPersistenceError("会话主体不匹配")
        current = int(record["state_summary"].get("covered_until", 0))
        if current != expected_covered_until:
            raise SummaryConflictError("摘要覆盖位置发生并发变化")
        record["state_summary"] = {
            "content": mask_text(summary, 1000),
            "covered_until": covered_until,
        }
