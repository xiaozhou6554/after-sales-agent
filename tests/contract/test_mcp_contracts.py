"""MCP 信封契约测试：请求/响应不变量与工具注册表（以 mcp-tools.json 为唯一源）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from interface.mcp.schemas.envelope import (
    MCPRequestModel,
    MCPResponseModel,
    ToolRegistrationModel,
)
from tests.contract.samples import (
    MCP_REQUEST,
    MCP_RESPONSE_ERROR,
    MCP_RESPONSE_SUCCESS,
    TOOL_REGISTRATION,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS_PATH = PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"


def test_request_sample_validates() -> None:
    model = MCPRequestModel.model_validate(copy.deepcopy(MCP_REQUEST))
    assert model.status == "request"
    assert model.error_code is None


def test_request_cannot_carry_auth_context() -> None:
    sample = copy.deepcopy(MCP_REQUEST)
    sample["auth_context"] = {"subject_id": "admin", "roles": ["admin"]}
    with pytest.raises(ValidationError):
        MCPRequestModel.model_validate(sample)


def test_request_status_frozen_to_request() -> None:
    sample = copy.deepcopy(MCP_REQUEST)
    sample["status"] = "success"
    with pytest.raises(ValidationError):
        MCPRequestModel.model_validate(sample)


def test_response_samples_validate() -> None:
    ok = MCPResponseModel.model_validate(copy.deepcopy(MCP_RESPONSE_SUCCESS))
    assert ok.data["order"]["amount_minor"] == 100000  # 金额整数分
    MCPResponseModel.model_validate(copy.deepcopy(MCP_RESPONSE_ERROR))


def test_error_response_requires_error_and_code() -> None:
    sample = copy.deepcopy(MCP_RESPONSE_ERROR)
    sample["error"] = None
    with pytest.raises(ValidationError):
        MCPResponseModel.model_validate(sample)
    sample = copy.deepcopy(MCP_RESPONSE_ERROR)
    sample["error_code"] = None
    with pytest.raises(ValidationError):
        MCPResponseModel.model_validate(sample)


def test_success_response_forbids_error() -> None:
    sample = copy.deepcopy(MCP_RESPONSE_SUCCESS)
    sample["error"] = dict(MCP_RESPONSE_ERROR["error"])
    with pytest.raises(ValidationError):
        MCPResponseModel.model_validate(sample)


def test_unknown_field_rejected_in_envelope() -> None:
    sample = copy.deepcopy(MCP_REQUEST)
    sample["raw_tool_output"] = {"internal": "data"}
    with pytest.raises(ValidationError):
        MCPRequestModel.model_validate(sample)


def test_tool_registration_model_validates_sample() -> None:
    model = ToolRegistrationModel.model_validate(dict(TOOL_REGISTRATION))
    assert model.access == "read"
    assert model.side_effect == "none"


def test_tool_registration_rejects_illegal_access_or_side_effect() -> None:
    bad = dict(TOOL_REGISTRATION, access="admin")
    with pytest.raises(ValidationError):
        ToolRegistrationModel.model_validate(bad)
    bad = dict(TOOL_REGISTRATION, side_effect="hidden")
    with pytest.raises(ValidationError):
        ToolRegistrationModel.model_validate(bad)


def test_real_mcp_tools_manifest_matches_registration_contract() -> None:
    """data/contracts/mcp-tools.json 的全部工具必须通过注册模型校验（唯一源）。"""
    manifest = json.loads(MCP_TOOLS_PATH.read_text(encoding="utf-8"))
    tools = manifest["tools"]
    assert len(tools) == 24
    for tool in tools:
        ToolRegistrationModel.model_validate(tool)
