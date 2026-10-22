"""本地 MCP 仅开放退款只读工具；无金额组成时不生成报价。"""

from infrastructure.mcp.local_probe import MCPServerProcess, free_port, probe_trace


def test_refund_read_tools_are_authorized_and_fail_closed() -> None:
    process = MCPServerProcess(free_port())
    try:
        process.wait_ready()
        token = process.login("cust-north-01")
        policy = process.call(
            "check_refund_policy",
            {"order_id": "ORDER-001", "reason_code": "changed_mind"},
            token=token,
            trace=probe_trace("refund-policy"),
        )
        assert policy["status"] == "partial"
        assert policy["data"]["eligibility"] == "unknown"
        assert policy["data"]["can_submit"] is False
        quote = process.call(
            "calculate_refund",
            {
                "order_id": "ORDER-001",
                "reason_code": "changed_mind",
                "lines": [{"order_line_id": "LINE-001", "quantity": 1}],
            },
            token=token,
            trace=probe_trace("refund-quote-unavailable"),
        )
        assert quote["status"] == "error" and quote["error_code"] == "STATE_CONFLICT"
        denied = process.call(
            "check_refund_policy",
            {"order_id": "ORDER-002", "reason_code": "changed_mind"},
            token=token,
            trace=probe_trace("refund-other-owner"),
        )
        assert denied["status"] == "error"
        assert denied["error_code"] == "RESOURCE_UNAVAILABLE"
        missing = process.call(
            "query_refund_request",
            {"refund_request_id": "refund-not-present"},
            token=token,
            trace=probe_trace("refund-request-missing"),
        )
        assert missing["status"] == "error"
        assert missing["error_code"] == "RESOURCE_UNAVAILABLE"
    finally:
        process.stop()
