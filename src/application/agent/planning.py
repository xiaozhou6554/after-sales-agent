"""复杂只读请求的受限顺序计划；计划本身从不授予工具权限。"""

from __future__ import annotations

from application.agent.ports.llm import LLMToolCall


def validate_tool_plan(
    calls: tuple[LLMToolCall, ...],
    *,
    is_allowed,
    max_tasks: int,
) -> tuple[dict, ...]:
    if len(calls) < 2 or len(calls) > max_tasks:
        raise ValueError("复杂计划任务数超出受限预算")
    ids = [call.call_id for call in calls]
    if any(not identifier for identifier in ids) or len(ids) != len(set(ids)):
        raise ValueError("计划任务标识缺失或重复")
    if any(not is_allowed(call.name) for call in calls):
        raise ValueError("计划包含未授权或未实现工具")
    return tuple(
        {
            "task_id": call.call_id,
            "tool_name": call.name,
            "arguments": dict(call.arguments or {}),
            "depends_on": [calls[index - 1].call_id] if index else [],
        }
        for index, call in enumerate(calls)
    )
