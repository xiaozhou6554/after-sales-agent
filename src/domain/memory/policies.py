"""记忆类别与留存策略。"""

from __future__ import annotations

from domain.memory.entities.memory_fact import MemoryFact

ALLOWED_FACT_TYPES = frozenset({"product_preference", "after_sales_fact"})


def acceptable_fact(fact: MemoryFact) -> bool:
    if fact.fact_type not in ALLOWED_FACT_TYPES:
        return False
    if not fact.fact_key or not fact.fact_value or not 0 <= fact.importance <= 1:
        return False
    forbidden = (
        "地址",
        "电话",
        "手机",
        "联系",
        "验证码",
        "密码",
        "银行卡",
        "身份证",
        "金额",
        "预算",
        "户型",
        "确认",
        "退款状态",
    )
    return not any(word in fact.fact_key or word in fact.fact_value for word in forbidden)
