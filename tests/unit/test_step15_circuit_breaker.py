"""熔断阈值、半开探测与恢复。"""

import time

from application.common.context import TraceContext
from application.common.execution.circuit_breaker import CircuitBreaker
from application.common.execution.timeout_policy import bind_deadline_at
from infrastructure.mcp.client import MCPToolClient


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_five_failures_open_then_three_probes_close() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(clock)
    for _ in range(5):
        assert circuit.allow()
        circuit.record(failed=True)
    assert circuit.state == "open"
    assert not circuit.allow()
    clock.now = 30.0
    for _ in range(3):
        assert circuit.allow()
        circuit.record(failed=False)
    assert circuit.state == "closed"
    assert circuit.allow()


def test_half_open_failure_reopens_and_old_failures_expire() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(clock)
    for _ in range(4):
        assert circuit.allow()
        circuit.record(failed=True)
    clock.now = 31.0
    assert circuit.allow()
    circuit.record(failed=True)
    assert circuit.state == "closed"
    for _ in range(4):
        assert circuit.allow()
        circuit.record(failed=True)
    assert circuit.state == "open"
    clock.now = 61.0
    assert circuit.allow()
    circuit.record(failed=True)
    assert circuit.state == "open"


class FailingMCPClient(MCPToolClient):
    def __init__(self) -> None:
        super().__init__("http://mcp.test/mcp")
        self.attempts = 0

    async def _call_once(self, tool_name, arguments, session_token, trace):
        self.attempts += 1
        return {
            "status": "error",
            "error_code": "DEPENDENCY_UNAVAILABLE",
            "error": {"retryable": True},
        }


async def test_mcp_circuit_stops_calls_and_parent_deadline_stops_attempt() -> None:
    client = FailingMCPClient()
    trace = TraceContext("1.0", "req-step15", "corr-step15", "trace-step15")
    for _ in range(5):
        response = await client.call_tool(
            "query_order", {}, session_token="mock-token", trace=trace, access="write"
        )
        assert response["status"] == "error"
    assert client.attempts == 5
    await client.call_tool("query_order", {}, session_token="mock-token", trace=trace)
    assert client.attempts == 5

    another = FailingMCPClient()
    with bind_deadline_at(time.monotonic() - 1):
        response = await another.call_tool(
            "query_order", {}, session_token="mock-token", trace=trace
        )
    assert response["error_code"] == "TIMEOUT"
    assert another.attempts == 0
