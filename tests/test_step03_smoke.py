"""步骤03冒烟测试：包导入、存活响应及启动关闭。"""

import asyncio

from bootstrap.api_main import create_app
from fastapi.testclient import TestClient

from application.common.health import HealthReport, ReadinessCheckUseCase


class _StubReadiness(ReadinessCheckUseCase):
    """测试桩：不发起真实依赖探测。"""

    def __init__(self) -> None:
        super().__init__(probes=(), memory_fallback_allowed=False)

    def execute(self) -> HealthReport:  # noqa: D102 - 桩实现
        return HealthReport(status="ready", dependencies={})


def test_four_layers_and_bootstrap_importable() -> None:
    import bootstrap

    import application
    import domain
    import infrastructure
    import interface

    for module in (application, bootstrap, domain, infrastructure, interface):
        assert module.__doc__


def test_health_endpoints_with_lifecycle() -> None:
    app = create_app(readiness=_StubReadiness())
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").json() == {"status": "ready", "dependencies": {}}


def test_mcp_server_factory_registers_only_implemented_tools() -> None:
    """不伪装未实现工具：无处理器时注册表为空（步骤09 起按注册表注册）。"""
    from bootstrap.container import MCP_TOOLS_PATH

    from infrastructure.mcp.server_factory import create_mcp_server

    server = create_mcp_server({}, registry_path=MCP_TOOLS_PATH)
    assert asyncio.run(server.list_tools()) == []
