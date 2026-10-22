"""敏感字段脱敏（技术详细规格 16.2、20.1、532/543 行约束，纯 Python）。

- 地址、联系方式、验证码、密钥及原始工具响应不得进入日志、审计、Redis 或消息体；
- 一切写入 MySQL 的工具参数/结果摘要先经本模块脱敏；
- 掩码为定值 "***"，不做可逆编码；截断显式标记，不无声丢弃。
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***"
TRUNCATED_SUFFIX = "...[已截断]"

# 键名匹配：不区分大小写的子串命中即整体掩码（键名与值一并视为敏感）。
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "otp",
    "captcha",
    "verify_code",
    "verification_code",
    "code_",
    "phone",
    "mobile",
    "contact",
    "address",
    "recipient",
    "receiver",
    "consignee",
    "id_card",
    "idcard",
    "bank_card",
    "card_no",
    "card_number",
    "account_no",
    "地址",
    "联系",
    "手机",
    "电话",
    "验证码",
    "银行卡",
    "身份证",
    "收件",
)

# 内容匹配：文本中的典型敏感模式（顺序即优先级，避免长数字被短模式截断）。
_CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{17}[\dXx]"),  # 身份证号
    re.compile(r"\b\d{13,19}\b"),  # 银行卡号等长卡号
    re.compile(r"\b1[3-9]\d{9}\b"),  # 大陆手机号
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),  # 邮箱
)

MAX_DEPTH = 6
MAX_ITEMS = 200
MAX_STR = 2000


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def mask_text_content(text: str) -> str:
    masked = text
    for pattern in _CONTENT_PATTERNS:
        masked = pattern.sub(MASK, masked)
    return masked


def mask_text(text: str, max_length: int = MAX_STR) -> str:
    """内容脱敏 + 有界截断；截断显式标记。"""
    clipped = text if len(text) <= max_length else text[:max_length] + TRUNCATED_SUFFIX
    return mask_text_content(clipped)


def mask_mapping(obj: Any, *, max_depth: int = MAX_DEPTH, max_items: int = MAX_ITEMS) -> Any:
    """深拷贝式脱敏：敏感键整体掩码，文本内容脱敏并截断，结构与有界性保持。"""
    return _mask(obj, depth=0, max_depth=max_depth, max_items=max_items)


def _mask(obj: Any, *, depth: int, max_depth: int, max_items: int) -> Any:
    if depth > max_depth:
        return MASK
    if isinstance(obj, dict):
        bounded = list(obj.items())[:max_items]
        result = {}
        for key, value in bounded:
            if is_sensitive_key(str(key)):
                result[key] = MASK
            else:
                result[key] = _mask(
                    value, depth=depth + 1, max_depth=max_depth, max_items=max_items
                )
        if len(obj) > max_items:
            result["_truncated_fields"] = len(obj) - max_items
        return result
    if isinstance(obj, (list, tuple)):
        bounded = list(obj)[:max_items]
        result = [
            _mask(item, depth=depth + 1, max_depth=max_depth, max_items=max_items)
            for item in bounded
        ]
        if len(obj) > max_items:
            result.append({"_truncated_items": len(obj) - max_items})
        return result
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, float)):
        return obj
    return mask_text(str(obj))
