"""FR-22 退款执行纯域规则：分摊行合法性与总额核算，不依赖外部库。"""

from __future__ import annotations

from dataclasses import dataclass

COMPONENTS = ("goods", "shipping")


class RefundAllocationError(ValueError):
    """分摊行非法或与执行金额不一致；调用方转为执行冲突。"""


@dataclass(frozen=True)
class AllocationLine:
    request_line_id: str
    component: str
    amount_minor: int


def validate_allocations(lines: list[dict]) -> list[AllocationLine]:
    """校验分摊行：正整数金额、合法资金组成、非空；支付明细由服务端解析。"""
    if not isinstance(lines, list) or not lines:
        raise RefundAllocationError("退款分摊不能为空")
    parsed: list[AllocationLine] = []
    for line in lines:
        if not isinstance(line, dict):
            raise RefundAllocationError("分摊行参数无效")
        line_id = line.get("request_line_id")
        component = line.get("component")
        amount = line.get("amount_minor")
        if not isinstance(line_id, str) or not line_id:
            raise RefundAllocationError("分摊行标识无效")
        if component not in COMPONENTS:
            raise RefundAllocationError("资金组成必须为 goods 或 shipping")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise RefundAllocationError("分摊金额必须为正整数分")
        parsed.append(AllocationLine(line_id, component, amount))
    return parsed


def allocation_total(lines: list[AllocationLine]) -> int:
    return sum(line.amount_minor for line in lines)
