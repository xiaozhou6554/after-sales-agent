"""MCP 进程入口：一期集中注册知识、订单、物流、退款、工单五类工具组。

- 步骤09 起按 ``data/contracts/mcp-tools.json`` 注册已实现工具（当前为订单
  工具组 query_order/query_payment），不伪装其他工具就绪；
- API/Worker 经 ``MCP_SERVER_URL`` 用同一 MCP 客户端访问；Mock 登录路由仅在
  开发/测试注册，生产环境双道防线（路由不注册 + 适配器拒绝签发）；
- 鉴权信息经受控传输请求头携带（Authorization 与链路三元组），服务端逐次
  二次授权。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bootstrap.container import MCP_TOOLS_PATH, build_mcp_runtime
from bootstrap.settings import load_settings
from infrastructure.mcp.server_factory import create_mcp_server
from infrastructure.observability.logging_setup import configure_logging
from interface.http.middleware.auth import install_error_handlers
from interface.http.routes.auth import router as auth_router


def build_mcp_app(settings) -> FastAPI:
    """组装 MCP 进程 ASGI 应用：/health/live + Mock 登录（dev）+ /mcp 端点。"""
    runtime = build_mcp_runtime(settings)
    mcp_asgi = create_mcp_server(
        runtime.tool_handlers,
        registry_path=MCP_TOOLS_PATH,
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    ).streamable_http_app()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 挂载的 MCP 子应用不会自动获得 lifespan：由父应用显式驱动会话管理器。
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            yield
        runtime.close()

    app = FastAPI(title="after-sales-mcp", lifespan=_lifespan)
    app.state.settings = settings
    app.state.auth = runtime.auth_gateway
    app.state.metrics = None
    install_error_handlers(app)

    @app.get("/health/live")
    def live() -> JSONResponse:
        return JSONResponse(status_code=200, content={"status": "live"})

    if settings.app_env != "production":
        app.include_router(auth_router)
    app.mount("/", mcp_asgi)
    return app


def main() -> None:
    settings = load_settings()
    configure_logging()
    import uvicorn

    uvicorn.run(
        build_mcp_app(settings),
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )


if __name__ == "__main__":
    main()
