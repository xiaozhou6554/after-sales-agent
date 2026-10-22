"""API 进程入口：网关认证、Agent 编排与健康检查；业务工具调用经 MCP 客户端（步骤09起接入）。

进程启动不执行迁移、不争抢结构升级；基础设施资源在 lifespan 内组装并逆序关闭。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from application.notification.queries import NotificationQueryUseCase
from bootstrap.container import (
    RuntimeContainer,
    build_auth_gateway,
    build_chat_service,
    build_runtime_container,
    build_session_repository,
)
from bootstrap.settings import RuntimeSettings, load_settings
from infrastructure.notification.mysql_reader import MySQLNotificationReader
from infrastructure.observability.logging_setup import configure_logging
from infrastructure.observability.metrics import InMemoryMetricsCollector
from interface.http.middleware.auth import install_error_handlers
from interface.http.middleware.observability import install_observability
from interface.http.routes.address import router as address_router
from interface.http.routes.analytics import router as analytics_router
from interface.http.routes.auth import router as auth_router
from interface.http.routes.chat import router as chat_router
from interface.http.routes.health import router as health_router
from interface.http.routes.investigation import router as investigation_router
from interface.http.routes.notification import router as notification_router
from interface.http.routes.ontology import router as ontology_router
from interface.http.routes.payment import router as payment_router
from interface.http.routes.refund import router as refund_router
from interface.http.routes.tasks import router as tasks_router
from interface.http.routes.ticket_commands import router as ticket_commands_router
from interface.http.routes.ticket_confirmations import router as ticket_confirmations_router
from interface.http.routes.warehouse import router as warehouse_router
from interface.http.routes.workbench import router as workbench_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "readiness", None) is None:
        container: RuntimeContainer = build_runtime_container(app.state.settings)
        app.state.readiness = container.readiness
        app.state.container = container
    try:
        yield
    finally:
        repository = getattr(app.state, "session_repository", None)
        if repository is not None:
            repository.close()
        container = getattr(app.state, "container", None)
        if container is not None:
            container.close()


def create_app(
    settings: RuntimeSettings | None = None,
    readiness: object | None = None,
    chat_service=None,
    task_service=None,
    confirmation_use_case=None,
    refund_qualification=None,
    refund_submission=None,
    refund_confirmation=None,
    refund_query=None,
    refund_reviewer=None,
    refund_tool_intent=None,
    ticket_commands=None,
    ticket_tool_intent=None,
    human_handoff=None,
    handoff_fallback_lookup=None,
    warehouse_service=None,
    refund_execution=None,
    address_change=None,
    address_apply=None,
    notification_engine=None,
    workbench_queries=None,
    workbench_ticket_commands=None,
) -> FastAPI:
    app = FastAPI(title="after-sales-agent-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    app.state.background_chat_enabled = app.state.settings.app_env != "production"
    if readiness is not None:
        # 测试与工具注入：提供就绪用例时不组装真实基础设施资源。
        app.state.readiness = readiness
    app.state.auth = build_auth_gateway(app.state.settings)
    # 对话服务：测试可注入 Fake LLM/工具；默认按配置装配（未配置密钥时
    # 使用如实失败的占位网关，不伪装可用）。默认装配的会话仓储与 MySQL
    # Checkpoint 保存器（步骤12）随 lifespan 关闭；注入的对话服务由注入方
    # 自管资源。
    if chat_service is not None:
        app.state.chat_service = chat_service
        app.state.task_service = task_service
        app.state.confirmation_use_case = confirmation_use_case
        app.state.refund_qualification = refund_qualification
        app.state.refund_submission = refund_submission
        app.state.refund_confirmation = refund_confirmation
        app.state.refund_query = refund_query
        app.state.refund_reviewer = refund_reviewer
        app.state.refund_tool_intent = refund_tool_intent
        app.state.ticket_commands = ticket_commands
        app.state.ticket_tool_intent = ticket_tool_intent
        app.state.human_handoff = human_handoff
        app.state.handoff_fallback_lookup = handoff_fallback_lookup
        app.state.warehouse_service = warehouse_service
        app.state.refund_execution = refund_execution
        app.state.address_change = address_change
        app.state.address_apply = address_apply
        app.state.notification_queries = (
            NotificationQueryUseCase(MySQLNotificationReader(notification_engine))
            if notification_engine is not None else None
        )
        app.state.workbench_queries = workbench_queries
        app.state.workbench_ticket_commands = workbench_ticket_commands
    else:
        container = build_runtime_container(app.state.settings)
        app.state.container = container
        if readiness is None:
            # 注入方自带 readiness 时不覆盖（测试/工具注入语义）。
            app.state.readiness = container.readiness
        repository = build_session_repository(app.state.settings)
        app.state.session_repository = repository
        app.state.chat_service = build_chat_service(
            app.state.settings,
            session_repository=repository,
            checkpointer=container.checkpoint_saver,
            profile_repository=container.memory_profile_repository,
            tool_invocations=container.tool_invocations,
        )
        app.state.task_service = task_service or container.task_use_cases
        app.state.confirmation_use_case = confirmation_use_case or container.confirmation_use_case
        app.state.refund_qualification = refund_qualification or container.refund_qualification
        app.state.refund_submission = refund_submission or container.refund_submission
        app.state.refund_confirmation = refund_confirmation or container.refund_confirmation
        app.state.refund_query = refund_query or container.refund_query
        app.state.refund_reviewer = refund_reviewer or getattr(container, "refund_reviewer", None)
        app.state.refund_tool_intent = refund_tool_intent or container.refund_tool_intent
        app.state.ticket_commands = ticket_commands or container.ticket_commands
        app.state.ticket_tool_intent = ticket_tool_intent or container.ticket_tool_intent
        app.state.human_handoff = human_handoff or container.human_handoff
        app.state.handoff_fallback_lookup = (
            handoff_fallback_lookup or container.handoff_fallback_lookup
        )
        app.state.warehouse_service = warehouse_service or getattr(
            container, "warehouse_service", None
        )
        app.state.refund_execution = refund_execution or getattr(
            container, "refund_execution", None
        )
        app.state.ontology_release_service = getattr(container, "ontology_release_service", None)
        app.state.analytics_projection = getattr(container, "analytics_projection", None)
        app.state.investigation_service = getattr(container, "investigation_service", None)
        app.state.address_change = getattr(container, "address_change", None)
        app.state.address_apply = getattr(container, "address_apply", None)
        app.state.notification_queries = NotificationQueryUseCase(
            MySQLNotificationReader(container.engine)
        )
        app.state.workbench_queries = workbench_queries or container.workbench_queries
        app.state.workbench_ticket_commands = (
            workbench_ticket_commands or container.workbench_ticket_commands
        )
    app.state.metrics = InMemoryMetricsCollector()
    install_error_handlers(app)
    install_observability(app)
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(tasks_router)
    app.include_router(ticket_confirmations_router)
    # Mock 登录仅开发/测试启用；生产不注册该入口，服务端也不接受客户端角色。
    if app.state.settings.app_env != "production":
        app.include_router(auth_router)
        app.include_router(refund_router)
        app.include_router(warehouse_router)
        app.include_router(payment_router)
        app.include_router(ticket_commands_router)
        app.include_router(ontology_router)
        app.include_router(analytics_router)
        app.include_router(investigation_router)
        app.include_router(address_router)
        app.include_router(notification_router)
        app.include_router(workbench_router)
    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if (frontend / "index.html").is_file():
        app.mount("/src", StaticFiles(directory=frontend / "src"), name="frontend-src")

        @app.get("/", include_in_schema=False)
        def customer_page() -> FileResponse:
            return FileResponse(frontend / "index.html")

    return app


def main() -> None:
    settings = load_settings()
    configure_logging()
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
