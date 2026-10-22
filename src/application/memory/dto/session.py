"""会话摘要及其覆盖范围。"""

from __future__ import annotations

from dataclasses import dataclass

from application.agent.ports.llm import ChatMessage


@dataclass(frozen=True)
class SessionSnapshot:
    messages: tuple[ChatMessage, ...]
    message_offset: int
    summary: str
    covered_until: int


class SummaryConflictError(RuntimeError):
    """摘要写入的已覆盖位置过期。"""
