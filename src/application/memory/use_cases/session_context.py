"""按对话轮数摘要旧消息并组装有界上下文。"""

from __future__ import annotations

import logging
import re

from application.agent.ports.llm import ChatMessage, LLMGateway
from application.common.masking import mask_text
from application.memory.dto.session import SummaryConflictError
from application.memory.ports.summary import SessionSummaryPort

logger = logging.getLogger("after_sales.memory")
SUMMARY_TRIGGER_ROUNDS = 6
RAW_WINDOW_ROUNDS = 4


class SessionContextUseCase:
    def __init__(self, repository: SessionSummaryPort, llm: LLMGateway) -> None:
        self._repository = repository
        self._llm = llm

    def history(self, tenant_id: str, thread_id: str, subject_id: str) -> list[ChatMessage]:
        snapshot = self._repository.load_snapshot(tenant_id, thread_id, subject_id=subject_id)
        messages = list(snapshot.messages)
        complete = 0
        for index in range(0, len(messages) - 1, 2):
            if messages[index].role == "user" and messages[index + 1].role == "assistant":
                complete += 1
            else:
                break
        summary = snapshot.summary
        covered_until = snapshot.covered_until
        if complete > SUMMARY_TRIGGER_ROUNDS:
            target = snapshot.message_offset + max(0, len(messages) - 2 * RAW_WINDOW_ROUNDS)
            if target > covered_until:
                start = max(0, covered_until - snapshot.message_offset)
                old = messages[start : target - snapshot.message_offset]
                try:
                    new_summary = self._summarize(summary, old)
                    self._repository.save_summary(
                        tenant_id,
                        thread_id,
                        subject_id=subject_id,
                        expected_covered_until=covered_until,
                        covered_until=target,
                        summary=new_summary,
                    )
                    summary, covered_until = new_summary, target
                except (RuntimeError, ValueError, SummaryConflictError):
                    logger.exception("会话摘要失败，使用原有摘要及历史裁剪")
        first = max(0, covered_until - snapshot.message_offset)
        raw = messages[first:]
        if summary:
            return [ChatMessage(role="user", content="[会话摘要，仅作上下文] " + summary), *raw]
        return raw

    def _summarize(self, previous: str, new_messages: list[ChatMessage]) -> str:
        instruction = (
            "压缩售后对话中的稳定上下文和未完成问题，禁止写入完整地址、联系方式、密钥、验证码、"
            "身份和确认信息。摘要是上下文，不是权限或实时业务状态。请只输出一段中文摘要。"
        )
        content = "已有摘要: " + previous + "\n新增对话:\n"
        content += "\n".join(
            f"{item.role}: {self._safe_text(item.content)}" for item in new_messages
        )
        result = self._llm.complete(
            system=instruction,
            messages=[ChatMessage(role="user", content=content)],
            tools=None,
        )
        summary = mask_text((result.content or "").strip(), 1000)
        if not summary or self._safe_text(summary) != summary:
            raise ValueError("模型未生成有效摘要")
        return summary

    @staticmethod
    def _safe_text(content: str) -> str:
        masked = mask_text(content)
        if "***" in masked or re.search(
            r"地址|收件|联系|手机|电话|验证码|密码|\d{1,4}号|(?:省|市|区).*(?:路|街|号|室)",
            masked,
        ):
            return "[敏感业务信息已略]"
        return masked
