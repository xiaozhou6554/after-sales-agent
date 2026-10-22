"""步骤22：真实 MCP 网络与 MySQL 订单归属守卫下的物流只读工具。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bootstrap.settings import load_settings
from jsonschema import Draft202012Validator
from sqlalchemy import text

from infrastructure.persistence.mysql.connection import create_mysql_engine
from tests.integration.test_step09_mcp_e2e import MCPServerProcess, _free_port, _trace

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "contracts" / "mcp-response.schema.json"
)
SCHEMA = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def server():
    process = MCPServerProcess(_free_port())
    try:
        process.wait_ready()
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def audit_cleanup():
    yield
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM agent_audit_log WHERE request_id LIKE 'req-e2e-tracking-ok-%'"
                " OR request_id LIKE 'req-e2e-log-ok-%'"
                " OR request_id LIKE 'req-e2e-log-denied-%'"
                " OR request_id LIKE 'req-e2e-damage-%'"
            )
        )
    engine.dispose()


def test_authorized_tracking_and_order_summary(server, audit_cleanup) -> None:
    token = server.login("cust-north-01")
    tracking = server.call(
        "query_tracking",
        {"tracking_number": "MOCK-TRACK-N-001"},
        token=token,
        trace=_trace("tracking-ok"),
    )
    SCHEMA.validate(tracking)
    assert tracking["status"] == "success"
    assert tracking["data"]["estimated_delivery_at"] is None
    assert tracking["data"]["events"][-1]["status"] == "delivered"
    summary = server.call(
        "query_logistics", {"order_id": "ORDER-001"}, token=token, trace=_trace("log-ok")
    )
    SCHEMA.validate(summary)
    assert summary["status"] == "success"
    assert len(summary["data"]["shipments"]) == 1


def test_cross_customer_and_invalid_reference_are_denied(server, audit_cleanup) -> None:
    token = server.login("cust-north-01")
    for name, arguments in (
        ("query_tracking", {"tracking_number": "MOCK-TRACK-N-002"}),
        ("query_logistics", {"order_id": "ORDER-002"}),
        ("query_logistics", {}),
    ):
        envelope = server.call(name, arguments, token=token, trace=_trace("log-denied"))
        SCHEMA.validate(envelope)
        assert envelope["status"] == "error"


def test_explicit_damage_exception_is_explained(server, audit_cleanup) -> None:
    token = server.login("cust-north-02")
    result = server.call(
        "check_delivery_exception",
        {"order_id": "ORDER-002"},
        token=token,
        trace=_trace("damage"),
    )
    SCHEMA.validate(result)
    assert result["status"] == "success"
    assert result["data"]["has_exception"] is True
    assert result["data"]["exceptions"][0]["exception_code"] == "PACKAGE_DAMAGED"
