"""退款资格未知是 FR-07.1 正式结果，不得被传输 Schema 错误拒绝。"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def test_refund_policy_response_accepts_unknown_eligibility() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "data/contracts/mcp-response.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["$defs"]["tool_data_check_refund_policy"])
    response = {
        "eligibility": "unknown",
        "can_submit": False,
        "reason_code": "PAYMENT_UNVERIFIED",
        "policy_id": "MOCK-FR-07.1",
        "policy_version": "2026-10-v1",
        "order_version": "3",
        "evidence_refs": [],
        "missing_information": [],
    }
    assert list(validator.iter_errors(response)) == []
