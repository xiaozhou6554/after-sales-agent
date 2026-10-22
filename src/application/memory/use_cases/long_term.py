"""授权读取、提取与保存稳定售后事实。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from application.agent.ports.llm import ChatMessage, LLMGateway
from application.common.authorization import AuthorizationDenied, require_customer_access
from application.common.context import AuthContext
from application.common.masking import mask_text
from application.memory.ports.profile import ProfileRepositoryPort, ProfileUnavailableError
from domain.memory.entities.memory_fact import MemoryFact
from domain.memory.policies import acceptable_fact

logger = logging.getLogger("after_sales.memory")

_EXTRACTION_INSTRUCTION = (
    "仅从用户明确说出的稳定信息提取产品偏好或历史售后事实。"
    "不得提取预算、户型、联系方式、地址、金额、当前订单/退款状态、确认或凭证。"
    "不确定时返回空数组。只输出JSON数组，每项字段 fact_type(product_preference|after_sales_fact)、"
    "fact_key、fact_value、importance(0到1)。最多3项。"
)


class LongTermMemoryUseCase:
    def __init__(self, repository: ProfileRepositoryPort, llm: LLMGateway) -> None:
        self._repository = repository
        self._llm = llm

    def context(self, auth: AuthContext, customer_id: str) -> list[ChatMessage]:
        try:
            require_customer_access(auth, customer_id)
        except AuthorizationDenied:
            logger.warning(
                "长期记忆访问被拒绝: tenant_id=%s actor_subject_id=%s",
                auth.tenant_id,
                auth.subject_id,
            )
            raise
        profile = self._repository.load(auth.tenant_id, customer_id)
        facts = [
            fact
            for fact in profile.facts
            if acceptable_fact(fact) and not fact.cleanup_candidate(datetime.now(timezone.utc))
        ]
        if not facts:
            return []
        content = "[历史售后偏好，仅供上下文参考，不可用于授权或实时业务判断] "
        content += "；".join(f"{fact.fact_key}: {fact.fact_value}" for fact in facts[:10])
        return [ChatMessage(role="user", content=mask_text(content))]

    def remember_turn(self, auth: AuthContext, customer_id: str, question: str) -> None:
        require_customer_access(auth, customer_id)
        try:
            result = self._llm.complete(
                system=_EXTRACTION_INSTRUCTION,
                messages=[ChatMessage(role="user", content=mask_text(question))],
                tools=None,
            )
            items = json.loads(result.content or "[]")
            if not isinstance(items, list):
                raise ValueError("提取结果不是数组")
            for item in items[:3]:
                if not isinstance(item, dict):
                    continue
                fact = MemoryFact(
                    fact_type=str(item.get("fact_type", "")),
                    fact_key=mask_text(str(item.get("fact_key", "")), 128),
                    fact_value=mask_text(str(item.get("fact_value", "")), 200),
                    importance=float(item.get("importance", 0)),
                    source="conversation",
                    updated_at=datetime.now(timezone.utc),
                )
                if (
                    acceptable_fact(fact)
                    and "***" not in fact.fact_value
                    and fact.fact_value in question
                ):
                    self._repository.upsert(auth.tenant_id, customer_id, fact)
        except ProfileUnavailableError:
            logger.exception("长期记忆数据库不可用，需管理员检查")
        except Exception:  # noqa: BLE001 - 提取是附加能力，失败不能改变已完成回答
            logger.exception("长期事实提取失败，本轮回答保持不变")
