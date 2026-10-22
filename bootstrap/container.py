"""按需组装资源、健康状态和逆序关闭；bootstrap 只组装，不承载业务规则。

资源对象在此惰性创建（引擎/客户端构造不发起网络连接），真正的连接发生在
首次使用或就绪探测时；进程启动不执行迁移，迁移由 bootstrap.migrate 显式执行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.analytics.projection_service import AnalyticsProjectionService
from application.common.audit import AuditUseCase
from application.common.health import ReadinessCheckUseCase
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.common.ports.dependency_probe import DependencyProbe
from application.common.tasks.use_cases import TaskUseCases
from application.investigation.service import InvestigationService
from application.knowledge.hybrid import HybridRetriever
from application.knowledge.policy import PolicyReader
from application.logistics.use_cases.logistics_query import QueryLogisticsUseCase
from application.memory.use_cases.long_term import LongTermMemoryUseCase
from application.memory.use_cases.session_context import SessionContextUseCase
from application.ontology.release_service import OntologyReleaseService
from application.order.use_cases.address_change import (
    AddressChangeApplyUseCase,
    AddressChangeUseCase,
)
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from application.refund.confirmation import RefundConfirmationUseCase
from application.refund.qualification import RefundQualificationUseCase
from application.refund.query import RefundRequestQueryUseCase
from application.refund.submission import RefundSubmissionUseCase
from application.ticket.handoff import MockHumanHandoff
from application.ticket.service import TicketApplicationService
from application.workbench.commands import WorkbenchTicketCommands
from application.workbench.queries import WorkbenchQueries
from bootstrap.local_knowledge_rag import LocalCandidateRagRetriever
from infrastructure.agent.budgeted_gateway import BudgetedLLMGateway
from infrastructure.agent.deepseek_adapter import DeepSeekChatGateway, UnconfiguredLLMGateway
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.agent.redis_session_repository import (
    FallbackSessionRepository,
    RedisSessionRepository,
)
from infrastructure.agent.tool_adapter import MCPToolPort
from infrastructure.analytics.mysql_projection import MySQLAnalyticsProjectionAdapter
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.document.policy_catalog import LocalManifestPolicySource
from infrastructure.graph.neo4j_adapter import Neo4jGraphAdapter
from infrastructure.investigation.mysql_service import MySQLInvestigationAdapter
from infrastructure.mcp.client import MCPToolClient
from infrastructure.mcp.server_factory import (
    build_address_read_tool_handlers,
    build_address_write_tool_handlers,
    build_analytics_read_tool_handlers,
    build_investigation_tool_handlers,
    build_knowledge_tool_handlers,
    build_logistics_tool_handlers,
    build_order_tool_handlers,
    build_payment_read_tool_handlers,
    build_refund_read_tool_handlers,
    build_refund_write_tool_handlers,
    build_ticket_read_tool_handlers,
    build_ticket_write_tool_handlers,
    build_warehouse_read_tool_handlers,
    load_tool_registry,
)
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.messaging.rabbitmq import RabbitMQDependencyProbe
from infrastructure.mock.logistics_adapter import MockLogisticsAdapter
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.ontology.mysql_identity_mapping import MySqlIdentityMapping
from infrastructure.ontology.mysql_release_repository import (
    MySqlOntologyReleaseRepository,
)
from infrastructure.ontology.mysql_release_service import MySQLOntologyReleaseAdapter
from infrastructure.order.mysql_address_change import (
    AddressCrypto,
    MySqlAddressChangeStore,
    MySqlAddressSnapshotAdapter,
    MySqlAddressToolIntent,
    MySqlMockAddressChangeAdapter,
)
from infrastructure.order.woocommerce_adapter import WooCommerceOrderAdapter
from infrastructure.payment.mock_refund_channel import MockRefundChannelAdapter
from infrastructure.payment.mysql_refund_execution_service import MySqlRefundExecutionService
from infrastructure.persistence.mysql.connection import MySQLDependencyProbe, create_mysql_engine
from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLActiveKnowledgeReleaseRepository,
)
from infrastructure.persistence.mysql.repositories.memory_profile_repository import (
    MySQLMemoryProfileRepository,
)
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.persistence.mysql.repositories.refund_request_read_repository import (
    MySqlRefundRequestReadRepository,
)
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository
from infrastructure.persistence.mysql.repositories.tool_invocation_repository import (
    MySQLToolInvocationRepository,
)
from infrastructure.persistence.neo4j.connection import (
    Neo4jDependencyProbe,
    create_neo4j_driver,
)
from infrastructure.persistence.redis.connection import (
    RedisDependencyProbe,
    create_redis_client,
)
from infrastructure.refund.human_review import MySqlMockRefundHumanReview
from infrastructure.refund.mysql_tool_intent import MySqlRefundToolIntent
from infrastructure.refund.submission_adapter import MySqlMockRefundSubmissionAdapter
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.ticket.mysql_confirmed_writer import MySQLConfirmedTicketWriter
from infrastructure.ticket.mysql_handoff_queue import MySQLHandoffQueue
from infrastructure.ticket.mysql_tool_intent import MySQLTicketToolIntentStore
from infrastructure.warehouse.mock_wms_adapter import MockWarehouseReturnAdapter
from infrastructure.warehouse.mysql_service import MySqlWarehouseService
from infrastructure.workbench.mock_staff_directory import MockStaffDirectory
from infrastructure.workbench.mysql_queries import (
    MySQLWorkbenchQueries,
    MySQLWorkbenchTicketCommand,
)
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_IDENTITIES_PATH = _PROJECT_ROOT / "data" / "fixtures" / "mock-identities.json"
MCP_TOOLS_PATH = _PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"
PROMPTS_PATH = _PROJECT_ROOT / "data" / "prompts"
EVENT_CATALOG_PATH = _PROJECT_ROOT / "data" / "contracts" / "event-catalog.json"
EVENT_SCHEMA_PATH = _PROJECT_ROOT / "data" / "contracts" / "events.schema.json"


class RuntimeContainer:
    """持有进程级资源与就绪用例；close 按创建的逆序释放。"""

    @property
    def engine(self):
        return self._mysql_engine

    def __init__(self, settings) -> None:
        self.settings = settings
        self._mysql_engine = create_mysql_engine(
            settings.mysql_url,
            settings.mysql_connect_timeout_s,
            settings.mysql_read_timeout_s,
        )
        self._redis_client = create_redis_client(
            settings.redis_url,
            settings.redis_connect_timeout_s,
            settings.redis_read_timeout_s,
        )
        self._neo4j_driver = create_neo4j_driver(
            settings.neo4j_uri,
            settings.neo4j_username,
            settings.neo4j_password,
            settings.neo4j_connection_timeout_s,
        )
        probes: tuple[DependencyProbe, ...] = (
            MySQLDependencyProbe(self._mysql_engine),
            RedisDependencyProbe(self._redis_client),
            Neo4jDependencyProbe(self._neo4j_driver, settings.neo4j_database),
            RabbitMQDependencyProbe(
                settings.rabbitmq_url,
                settings.rabbitmq_connect_timeout_s,
                settings.rabbitmq_read_timeout_s,
            ),
        )
        # 内存降级仅允许显式开启且非生产（生产场景已在配置层拒绝）。
        fallback_allowed = settings.memory_fallback_enabled and settings.app_env != "production"
        self.readiness = ReadinessCheckUseCase(
            probes=probes, memory_fallback_allowed=fallback_allowed
        )
        # 审计用例：事前意图/事后结果；摘要脱敏在用例内统一执行。
        self.audit = AuditUseCase(MySQLAuditRepository(self._mysql_engine))
        self.confirmation_use_case = OperationConfirmationUseCase(
            MySQLConfirmationStore(self._mysql_engine), settings.ticket_confirm_ttl_seconds
        )
        # Agent Checkpoint 保存器（步骤12）：与就绪探测共享 MySQL 引擎，
        # 引擎随 close 释放，保存器自身不另持连接。
        self.checkpoint_saver = MySQLCheckpointSaver(self._mysql_engine)
        self.tool_invocations = MySQLToolInvocationRepository(self._mysql_engine)
        self.memory_profile_repository = MySQLMemoryProfileRepository(self._mysql_engine)
        event_catalog = EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)
        self.task_use_cases = TaskUseCases(
            MySQLTaskRepository(
                self._mysql_engine,
                OutboxRepository(self._mysql_engine, event_catalog),
            )
        )
        self.refund_qualification = None
        self.refund_submission = None
        self.refund_confirmation = None
        self.refund_query = None
        self.refund_reviewer = None
        self.refund_tool_intent = None
        self.address_change = None
        self.address_apply = None
        self.address_tool_intent = None
        self.ticket_commands = None
        self.ticket_tool_intent = None
        self.human_handoff = None
        self.handoff_fallback_lookup = None
        self.warehouse_service = None
        self.refund_execution = None
        self.ontology_release_service = None
        self.analytics_projection = None
        self.investigation_service = None
        self.workbench_queries = None
        self.workbench_ticket_commands = None
        if settings.app_env != "production":
            self.workbench_queries = WorkbenchQueries(MySQLWorkbenchQueries(self._mysql_engine))
            self.workbench_ticket_commands = WorkbenchTicketCommands(
                self.workbench_queries,
                self.confirmation_use_case,
                MySQLWorkbenchTicketCommand(self._mysql_engine),
                MockStaffDirectory(),
            )
            self.warehouse_service = MySqlWarehouseService(
                self._mysql_engine,
                MockWarehouseReturnAdapter(self._mysql_engine),
                OutboxRepository(self._mysql_engine, event_catalog),
            )
            self.ticket_commands = TicketApplicationService(
                MySQLTicketRepository(self._mysql_engine),
                MySQLConfirmedTicketWriter(self._mysql_engine),
                MySQLConfirmationStore(self._mysql_engine),
                confirmation_ttl_seconds=settings.ticket_confirm_ttl_seconds,
            )
            self.refund_execution = MySqlRefundExecutionService(
                self._mysql_engine,
                MockRefundChannelAdapter(self._mysql_engine),
                OutboxRepository(self._mysql_engine, event_catalog),
            )
            identity_mapping = MySqlIdentityMapping(self._mysql_engine)
            self.ontology_release_service = OntologyReleaseService(
                MySQLOntologyReleaseAdapter(
                    self._mysql_engine,
                    MySqlOntologyReleaseRepository(
                        self._mysql_engine,
                        OutboxRepository(self._mysql_engine, event_catalog),
                    ),
                    identity_mapping,
                ),
            )
            self.analytics_projection = AnalyticsProjectionService(
                MySQLAnalyticsProjectionAdapter(self._mysql_engine, identity_mapping)
            )
            self.investigation_service = InvestigationService(
                MySQLInvestigationAdapter(self._mysql_engine, self.analytics_projection)
            )
            self.ticket_tool_intent = MySQLTicketToolIntentStore(
                self._mysql_engine, self.ticket_commands
            )
            handoff_store = MySQLHandoffQueue(self._mysql_engine)
            self.human_handoff = MockHumanHandoff(handoff_store)
            self.handoff_fallback_lookup = handoff_store.get
            refund_snapshots = MockRefundSnapshotAdapter(self._mysql_engine)
            refund_quotes = MySqlRefundQuoteRepository(self._mysql_engine)
            self.refund_qualification = RefundQualificationUseCase(
                refund_snapshots,
                refund_quotes,
                ttl_seconds=settings.refund_confirm_ttl_seconds,
            )
            self.refund_submission = RefundSubmissionUseCase(
                refund_quotes,
                MySqlMockRefundSubmissionAdapter(
                    self._mysql_engine,
                    refund_snapshots,
                    OutboxRepository(self._mysql_engine, event_catalog),
                ),
            )
            self.refund_confirmation = RefundConfirmationUseCase(
                self.refund_submission,
                MySQLConfirmationStore(self._mysql_engine),
                ttl_seconds=settings.refund_confirm_ttl_seconds,
            )
            self.refund_query = RefundRequestQueryUseCase(
                MySqlRefundRequestReadRepository(self._mysql_engine)
            )
            self.refund_reviewer = MySqlMockRefundHumanReview(
                self._mysql_engine,
                refund_snapshots,
                OutboxRepository(self._mysql_engine, event_catalog),
            )
            self.refund_tool_intent = MySqlRefundToolIntent(self._mysql_engine)
            address_store = MySqlAddressChangeStore(
                self._mysql_engine, AddressCrypto(settings.address_encrypt_key)
            )
            address_snapshots = MySqlAddressSnapshotAdapter(self._mysql_engine)
            self.address_tool_intent = MySqlAddressToolIntent(self._mysql_engine)
            self.address_change = AddressChangeUseCase(
                address_snapshots,
                address_store,
                OperationConfirmationUseCase(
                    MySQLConfirmationStore(self._mysql_engine),
                    settings.address_confirm_ttl_seconds,
                ),
                self.address_tool_intent,
                ttl_seconds=settings.address_confirm_ttl_seconds,
            )
            self.address_apply = AddressChangeApplyUseCase(
                address_store,
                MySqlMockAddressChangeAdapter(
                    self._mysql_engine,
                    address_store,
                    OutboxRepository(self._mysql_engine, event_catalog),
                ),
            )

    def run_all_probes(self) -> list:
        """供 infrastructure_check 使用：逐项探测并返回带错误信息的结果。"""
        return self.readiness.probe_all()

    def close(self) -> None:
        for resource in (self._neo4j_driver, self._redis_client, self._mysql_engine):
            try:
                resource.close()
            except Exception:  # noqa: BLE001 - 逆序关闭阶段不再向外抛出。
                pass


def build_runtime_container(settings) -> RuntimeContainer:
    return RuntimeContainer(settings)


def build_auth_gateway(settings) -> MockAuthAdapter:
    """Mock 认证网关：仅开发/测试启用 Mock 登录；生产由企业 IdP 适配器替换。"""
    return MockAuthAdapter.from_fixture(
        MOCK_IDENTITIES_PATH,
        enabled=settings.app_env != "production",
    )


def build_llm_gateway(settings, llm_gateway=None):
    """模型网关：测试注入优先；未配置密钥时用如实失败的占位实现。"""
    if llm_gateway is not None:
        return llm_gateway
    api_key = settings.llm_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return UnconfiguredLLMGateway()
    return DeepSeekChatGateway(
        api_key=api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        connect_timeout_s=settings.llm_connect_timeout_s,
        read_timeout_s=settings.llm_read_timeout_s,
    )


def build_session_repository(settings):
    """会话仓储装配：Redis 主仓储；仅显式降级开关下附带进程内兜底（15 章）。"""
    client = create_redis_client(
        settings.redis_url,
        settings.redis_connect_timeout_s,
        settings.redis_read_timeout_s,
    )
    primary = RedisSessionRepository(client)
    fallback_allowed = settings.memory_fallback_enabled and settings.app_env != "production"
    if not fallback_allowed:
        return primary
    return FallbackSessionRepository(primary, InMemorySessionRepository(), fallback_allowed=True)


def build_chat_service(
    settings,
    *,
    llm_gateway=None,
    session_repository=None,
    checkpointer=None,
    profile_repository=None,
    tool_invocations=None,
) -> ChatService:
    """对话服务装配：模型网关 + MCP 工具端口 + 会话仓储（默认 Redis）。

    checkpointer（步骤12 MySQLCheckpointSaver）由调用方注入；编排代码
    不自行创建数据库客户端。
    """
    client = MCPToolClient(
        settings.mcp_server_url,
        read_timeout_s=settings.mcp_read_timeout_s,
        invocations=tool_invocations,
    )
    llm = BudgetedLLMGateway(build_llm_gateway(settings, llm_gateway))
    memory = session_repository or build_session_repository(settings)
    if profile_repository is None:
        profile_engine = create_mysql_engine(
            settings.mysql_url,
            settings.mysql_connect_timeout_s,
            settings.mysql_read_timeout_s,
        )
        profile_repository = MySQLMemoryProfileRepository(profile_engine)
    return ChatService(
        llm=llm,
        tools=MCPToolPort(client),
        memory=memory,
        prompts=PromptCatalog.load(PROMPTS_PATH),
        registry_path=MCP_TOOLS_PATH,
        max_steps=settings.agent_max_steps,
        registered_tool_names=registered_read_tool_names(settings),
        checkpointer=checkpointer,
        session_context=SessionContextUseCase(memory, llm)
        if hasattr(memory, "load_snapshot")
        else None,
        long_term_memory=LongTermMemoryUseCase(profile_repository, llm),
    )


def registered_read_tool_names(settings) -> frozenset[str]:
    """与当前MCP装配对齐；契约中尚未实现的只读工具不进入模型清单。"""
    names = {
        "query_order",
        "query_payment",
        "query_tracking",
        "query_logistics",
        "check_delivery_exception",
        "query_ticket",
    }
    local_rag = (
        settings.app_env != "production"
        and settings.knowledge_local_tools_enabled
        and settings.vector_backend == "chroma"
    )
    graph = settings.neo4j_read_only and bool(settings.neo4j_query_password)
    if local_rag:
        names.update(("search_knowledge_base", "get_policy_detail"))
    if graph:
        names.add("query_knowledge_graph")
    if local_rag and graph:
        names.add("search_hybrid_knowledge")
    if settings.app_env != "production":
        names.update(
            (
                "check_refund_policy",
                "calculate_refund",
                "create_refund_request",
                "query_refund_request",
                "create_ticket",
                "update_ticket",
                "handoff_to_human",
                "query_return_progress",
                "query_refund_execution",
            )
        )
    return frozenset(names)


@dataclass(frozen=True)
class MCPRuntime:
    """MCP 进程装配结果：处理器已按注册表注册，引擎随 close 释放。"""

    settings: Any
    auth_gateway: MockAuthAdapter
    tool_handlers: dict
    mysql_engine: Any
    neo4j_query_driver: Any | None = None
    order_adapter: Any | None = None

    def close(self) -> None:
        if self.order_adapter is not None:
            close_adapter = getattr(self.order_adapter, "close", None)
            if callable(close_adapter):
                try:
                    close_adapter()
                except Exception:  # noqa: BLE001 - 关闭阶段不外抛
                    pass
        if self.neo4j_query_driver is not None:
            try:
                self.neo4j_query_driver.close()
            except Exception:  # noqa: BLE001 - 关闭阶段不外抛
                pass
        try:
            self.mysql_engine.dispose()
        except Exception:  # noqa: BLE001 - 关闭阶段不外抛
            pass


def build_order_query_adapter(settings, engine):
    """Select the order query port implementation at the composition root."""
    if settings.woo_order_queries_enabled:
        return WooCommerceOrderAdapter(
            settings.woo_store_url,
            settings.woo_consumer_key,
            settings.woo_consumer_secret,
        )
    return MockOrderAdapter(engine)


def build_mcp_runtime(settings) -> MCPRuntime:
    """组装独立 MCP 进程：订单与物流工具共用逐次授权及审计。"""
    registry = load_tool_registry(MCP_TOOLS_PATH)
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )
    adapter = build_order_query_adapter(settings, engine)
    order_query = QueryOrderUseCase(adapter)
    graph_driver = None
    if settings.neo4j_read_only and settings.neo4j_query_password:
        graph_driver = create_neo4j_driver(
            settings.neo4j_uri,
            settings.neo4j_query_username,
            settings.neo4j_query_password,
            settings.neo4j_connection_timeout_s,
        )
    local_knowledge = (
        settings.app_env != "production"
        and settings.knowledge_local_tools_enabled
        and settings.vector_backend == "chroma"
    )
    rag = LocalCandidateRagRetriever(settings) if local_knowledge else None
    active_release = MySQLActiveKnowledgeReleaseRepository(engine)
    if rag is not None:
        for tenant_id in ("tenant-north", "tenant-south"):
            release_id = active_release.get_active_release_id(tenant_id)
            if release_id is not None:
                rag.warm_up(tenant_id, release_id)
                break
    policy = (
        PolicyReader(
            LocalManifestPolicySource(
                _PROJECT_ROOT / "data/knowledge/v2/corpus.json",
                _PROJECT_ROOT / "data/manifests/knowledge-v2.json",
            )
        )
        if local_knowledge
        else None
    )
    graph = (
        Neo4jGraphAdapter(graph_driver, settings.neo4j_database)
        if graph_driver is not None
        else None
    )
    refund_snapshots = (
        MockRefundSnapshotAdapter(engine) if settings.app_env != "production" else None
    )
    refund_quotes = MySqlRefundQuoteRepository(engine) if refund_snapshots is not None else None
    deps = MCPToolDeps(
        auth_gateway=build_auth_gateway(settings),
        guard=ToolContextGuard(MCP_TOOLS_PATH),
        query_order=order_query,
        query_payment=QueryPaymentUseCase(adapter),
        audit=AuditUseCase(MySQLAuditRepository(engine)),
        query_logistics=QueryLogisticsUseCase(
            order_query,
            MockLogisticsAdapter(_PROJECT_ROOT / "data" / "mock" / "v2" / "logistics.json"),
        ),
        active_knowledge_release=active_release,
        rag_retriever=rag,
        graph_search=graph,
        hybrid_retriever=HybridRetriever(rag, graph) if rag and graph else None,
        policy_reader=policy,
        refund_qualification=(
            RefundQualificationUseCase(refund_snapshots, refund_quotes)
            if refund_snapshots is not None and refund_quotes is not None
            else None
        ),
        refund_request_query=(
            RefundRequestQueryUseCase(MySqlRefundRequestReadRepository(engine))
            if settings.app_env != "production"
            else None
        ),
        refund_submission=(
            RefundSubmissionUseCase(
                refund_quotes,
                MySqlMockRefundSubmissionAdapter(
                    engine,
                    refund_snapshots,
                    OutboxRepository(engine, EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)),
                ),
            )
            if refund_snapshots is not None and refund_quotes is not None
            else None
        ),
        refund_tool_intent=(
            MySqlRefundToolIntent(engine) if refund_snapshots is not None else None
        ),
        ticket_query=TicketApplicationService(
            MySQLTicketRepository(engine),
            MySQLConfirmedTicketWriter(engine),
            MySQLConfirmationStore(engine),
        ),
    )
    address_change = None
    address_apply = None
    address_tool_intent = None
    if settings.app_env != "production":
        address_store = MySqlAddressChangeStore(engine, AddressCrypto(settings.address_encrypt_key))
        address_tool_intent = MySqlAddressToolIntent(engine)
        address_change = AddressChangeUseCase(
            MySqlAddressSnapshotAdapter(engine),
            address_store,
            OperationConfirmationUseCase(
                MySQLConfirmationStore(engine), settings.address_confirm_ttl_seconds
            ),
            address_tool_intent,
            ttl_seconds=settings.address_confirm_ttl_seconds,
        )
        address_apply = AddressChangeApplyUseCase(
            address_store,
            MySqlMockAddressChangeAdapter(
                engine,
                address_store,
                OutboxRepository(engine, EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)),
            ),
        )
    deps = replace(
        deps,
        address_change=address_change,
        address_apply=address_apply,
        address_tool_intent=address_tool_intent,
    )
    if settings.app_env != "production":
        ticket_service = deps.ticket_query
        warehouse_service = MySqlWarehouseService(
            engine,
            MockWarehouseReturnAdapter(engine),
            OutboxRepository(engine, EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)),
        )
        refund_execution_service = MySqlRefundExecutionService(
            engine,
            MockRefundChannelAdapter(engine),
            OutboxRepository(engine, EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)),
        )
        identity_mapping = MySqlIdentityMapping(engine)
        analytics_projection = AnalyticsProjectionService(
            MySQLAnalyticsProjectionAdapter(engine, identity_mapping)
        )
        ontology_release_repo = MySqlOntologyReleaseRepository(engine)
        investigation_service = InvestigationService(
            MySQLInvestigationAdapter(engine, analytics_projection)
        )
        deps = replace(
            deps,
            ticket_tool_intent=MySQLTicketToolIntentStore(engine, ticket_service),
            human_handoff=MockHumanHandoff(MySQLHandoffQueue(engine)),
            human_handoff_available=True,
            warehouse_query=warehouse_service,
            refund_execution_query=refund_execution_service,
            analytics_metrics=analytics_projection,
            ontology_active_release=ontology_release_repo,
            investigation=investigation_service,
        )
    handlers = build_order_tool_handlers(deps, execute_tool_call)
    handlers.update(build_logistics_tool_handlers(deps, execute_tool_call))
    knowledge_handlers = build_knowledge_tool_handlers(deps, execute_tool_call)
    if deps.rag_retriever is not None:
        handlers["search_knowledge_base"] = knowledge_handlers["search_knowledge_base"]
    if deps.graph_search is not None:
        handlers["query_knowledge_graph"] = knowledge_handlers["query_knowledge_graph"]
    if deps.hybrid_retriever is not None:
        handlers["search_hybrid_knowledge"] = knowledge_handlers["search_hybrid_knowledge"]
    if deps.policy_reader is not None:
        handlers["get_policy_detail"] = knowledge_handlers["get_policy_detail"]
    if deps.refund_qualification is not None and deps.refund_request_query is not None:
        handlers.update(build_refund_read_tool_handlers(deps, execute_tool_call))
    if deps.refund_submission is not None and deps.refund_tool_intent is not None:
        handlers.update(build_refund_write_tool_handlers(deps, execute_tool_call))
    handlers.update(build_ticket_read_tool_handlers(deps, execute_tool_call))
    if deps.ticket_tool_intent is not None and deps.human_handoff is not None:
        handlers.update(build_ticket_write_tool_handlers(deps, execute_tool_call))
    if deps.warehouse_query is not None:
        handlers.update(build_warehouse_read_tool_handlers(deps, execute_tool_call))
    if deps.refund_execution_query is not None:
        handlers.update(build_payment_read_tool_handlers(deps, execute_tool_call))
    if deps.analytics_metrics is not None:
        handlers.update(build_analytics_read_tool_handlers(deps, execute_tool_call))
    if deps.investigation is not None:
        handlers.update(build_investigation_tool_handlers(deps, execute_tool_call))
    if deps.address_change is not None:
        handlers.update(build_address_read_tool_handlers(deps, execute_tool_call))
    if deps.address_apply is not None and deps.address_tool_intent is not None:
        handlers.update(build_address_write_tool_handlers(deps, execute_tool_call))
    unknown = set(handlers) - set(registry)
    if unknown:  # 不伪装注册表之外的工具
        raise ValueError(f"工具未在 mcp-tools.json 注册：{sorted(unknown)}")
    return MCPRuntime(
        settings=settings,
        auth_gateway=deps.auth_gateway,
        tool_handlers=handlers,
        mysql_engine=engine,
        neo4j_query_driver=graph_driver,
        order_adapter=adapter,
    )
