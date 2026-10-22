"""步骤08 脱敏单元测试：16.2 禁写清单、键名掩码、有界截断。"""

from __future__ import annotations

from application.common.masking import (
    MASK,
    TRUNCATED_SUFFIX,
    is_sensitive_key,
    mask_mapping,
    mask_text,
)


def test_text_patterns_masked() -> None:
    text = "客户 13812345678，邮箱 a.b@c.com，卡号 6222020200112233445，证件 110101199001011234"
    masked = mask_text(text)
    assert "13812345678" not in masked
    assert "a.b@c.com" not in masked
    assert "6222020200112233445" not in masked
    assert "110101199001011234" not in masked
    assert MASK in masked


def test_sensitive_keys_masked_case_insensitive() -> None:
    payload = {
        "Phone": "13812345678",
        "home_address": "北京市海淀区xx街1号",
        "VERIFY_CODE": "123456",
        "api_key": "sk-abc123",
        "Bank_Card_No": "6222020200112233445",
    }
    masked = mask_mapping(payload)
    for key in payload:
        assert masked[key] == MASK, key
    assert is_sensitive_key("recipient_phone")
    assert not is_sensitive_key("order_status")


def test_non_sensitive_structure_preserved() -> None:
    payload = {
        "order_id": "ORDER-001",
        "status": "delivered",
        "amount_minor": 100000,
        "lines": ["line-1", "line-2"],
        "paid": True,
        "note": None,
    }
    assert mask_mapping(payload) == payload


def test_nested_and_truncation_bounded() -> None:
    payload = {
        "tool_result": {"items": [{"message": "x" * 5000}], "contact_name": "张三"},
        "remark": "联系 13912345678",
    }
    masked = mask_mapping(payload)
    nested_message = masked["tool_result"]["items"][0]["message"]
    assert len(nested_message) <= 2000 + len(TRUNCATED_SUFFIX)
    assert nested_message.endswith(TRUNCATED_SUFFIX)
    assert masked["tool_result"]["contact_name"] == MASK
    assert "13912345678" not in masked["remark"]


def test_depth_limit_never_leaks_deep_payloads() -> None:
    deep = current = {}
    for level in range(10):
        current["child"] = {"phone": "13812345678", "level": level}
        current = current["child"]
    masked = mask_mapping(deep)
    assert MASK in str(masked)  # 超深结构整体掩码，不泄露


def test_list_truncation_marks_overflow() -> None:
    payload = {"ids": [f"id-{i}" for i in range(300)]}
    masked = mask_mapping(payload)
    assert len(masked["ids"]) == 201  # 200 条 + 截断标记
    assert masked["ids"][-1] == {"_truncated_items": 100}
