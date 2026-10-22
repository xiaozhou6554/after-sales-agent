"""所有模型调用共用的工作上下文预算守卫。"""

from __future__ import annotations

import json

from application.agent.ports.llm import ChatMessage, LLMGateway, LLMResult, ToolSpec

CONTEXT_TOKEN_LIMIT = 32000


class ContextBudgetError(RuntimeError):
    """系统规则及当前必要输入本身超过模型上下文上限。"""


class BudgetedLLMGateway:
    def __init__(self, gateway: LLMGateway, *, limit: int = CONTEXT_TOKEN_LIMIT) -> None:
        self._gateway = gateway
        self._limit = limit

    def complete(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> LLMResult:
        selected = list(messages)
        while self._count(system, selected, tools) > self._limit:
            removable = next(
                (
                    index
                    for index, item in enumerate(selected[:-1])
                    if item.role != "system" and len(selected) > 2
                ),
                None,
            )
            if removable is None:
                raise ContextBudgetError(f"必要模型上下文超过{self._limit} Token")
            selected.pop(removable)
        return self._gateway.complete(system=system, messages=selected, tools=tools)

    def _count(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None) -> int:
        # 适配器只发送一次与 system 参数相同的系统提示词。
        texts = [system]
        duplicate_system_skipped = False
        for item in messages:
            if item.role == "system" and item.content == system and not duplicate_system_skipped:
                duplicate_system_skipped = True
                continue
            texts.append(item.content)
        if tools:
            texts.extend(
                json.dumps(spec.parameters, ensure_ascii=False) + spec.name + spec.description
                for spec in tools
            )
        # UTF-8 字节数是字节级分词的保守上界，无需运行时下载编码表。
        return sum(len(value.encode("utf-8")) + 8 for value in texts) + 64
