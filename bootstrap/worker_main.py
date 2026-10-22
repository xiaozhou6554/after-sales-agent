"""Worker 进程入口：长期记忆清理与按所有者隔离的消息发布。"""

import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from application.agent.ports.memory import MemoryUnavailableError
from application.common.execution.invocation_scope import ToolInvocationPending
from application.common.tasks.models import TaskRecord
from application.knowledge.indexing.worker import KnowledgeIndexWorker
from application.memory.ports.profile import ProfileUnavailableError
from application.memory.use_cases.maintenance import MemoryMaintenanceUseCase
from bootstrap.container import (
    build_auth_gateway,
    build_chat_service,
    build_session_repository,
)
from bootstrap.lifecycle import StopAndWait
from bootstrap.settings import RuntimeSettings, load_settings
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import InboxRepository
from infrastructure.messaging.knowledge_update import (
    LocalReviewedManifestSource,
    knowledge_updated_handler,
)
from infrastructure.messaging.notification_handlers import (
    MockNotificationSender,
    NotificationFanoutHandler,
    NotificationSendHandler,
)
from infrastructure.messaging.outbox_publisher import OutboxPublisher
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.messaging.rabbitmq import RabbitMQTransport
from infrastructure.messaging.retry import InboxConsumer
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.knowledge_index_job_repository import (
    MySQLKnowledgeIndexJobRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_manifest_repository import (
    MySQLKnowledgeManifestRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLKnowledgeReleasePublisher,
)
from infrastructure.persistence.mysql.repositories.memory_profile_repository import (
    MySQLMemoryProfileRepository,
)
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository
from infrastructure.persistence.mysql.repositories.tool_invocation_repository import (
    MySQLToolInvocationRepository,
)
from infrastructure.refund.review_handler import MockRefundReviewHandler
from infrastructure.resilience.task_dispatcher import (
    TaskDeferred,
    TaskDispatcher,
    TaskOutcome,
    task_created_handler,
)

logger = logging.getLogger("after_sales.worker")
MEMORY_CLEANUP_INTERVAL_S = 86400
MESSAGE_POLL_INTERVAL_S = 0.5


def build_chat_task_handler(chat_service, auth_gateway) -> Callable[[TaskRecord], TaskOutcome]:
    """后台执行身份只从服务端账户配置重建，不持久化用户会话令牌。"""

    def run_chat_task(task: TaskRecord) -> TaskOutcome:
        grant = auth_gateway.issue_task_session(
            task.tenant_id, task.actor_subject_id, task.customer_id
        )
        try:
            return TaskOutcome(chat_service.resume_task(task, grant.auth, grant.session_token))
        except (ToolInvocationPending, MemoryUnavailableError) as exc:
            raise TaskDeferred("后台依赖暂不可用，保留原任务等待恢复") from exc

    return run_chat_task


def run_memory_cleanup(stopper: StopAndWait, repository) -> None:
    use_case = MemoryMaintenanceUseCase(repository)
    while not stopper.is_stopped():
        try:
            deleted = use_case.run()
            logger.info("长期记忆衰减清理完成: deleted=%d", deleted)
        except ProfileUnavailableError:
            logger.exception("长期记忆清理失败，下一周期重试并提示管理员")
        next_run = time.monotonic() + MEMORY_CLEANUP_INTERVAL_S
        while not stopper.is_stopped() and time.monotonic() < next_run:
            time.sleep(min(0.5, next_run - time.monotonic()))


def run_messaging(
    stopper: StopAndWait,
    engine,
    rabbitmq_url: str,
    task_handlers: dict[str, Callable[[TaskRecord], TaskOutcome]] | None = None,
    settings: RuntimeSettings | None = None,
) -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = EventCatalog(
        root / "data/contracts/event-catalog.json",
        root / "data/contracts/events.schema.json",
    )
    repository = OutboxRepository(engine, catalog)
    task_repository = MySQLTaskRepository(engine, repository)
    registered_handlers = task_handlers or {}
    local_knowledge = bool(
        settings is not None
        and settings.app_env != "production"
        and settings.knowledge_local_tools_enabled
        and settings.neo4j_query_password
    )
    if local_knowledge:
        from bootstrap.local_knowledge_indexing import (
            CORPUS,
            MANIFEST,
            LocalReviewedIndexBackend,
        )

    manifest_repository = MySQLKnowledgeManifestRepository(engine) if local_knowledge else None
    index_jobs = MySQLKnowledgeIndexJobRepository(engine) if local_knowledge else None
    knowledge_publisher = MySQLKnowledgeReleasePublisher(engine) if local_knowledge else None
    local_release_id = (
        json.loads(MANIFEST.read_bytes())["knowledge_release_id"] if local_knowledge else ""
    )
    worker_id = uuid.uuid4().hex
    while not stopper.is_stopped():
        transport = None
        try:
            transport = RabbitMQTransport(rabbitmq_url, catalog)
            publisher = OutboxPublisher(repository, transport, catalog)
            task_consumer = (
                InboxConsumer(
                    InboxRepository(engine, max_retries=catalog.max_retries),
                    transport,
                    catalog,
                    {"agent.task.created": task_created_handler},
                )
                if registered_handlers
                else None
            )
            knowledge_consumer = (
                InboxConsumer(
                    InboxRepository(engine, max_retries=catalog.max_retries),
                    transport,
                    catalog,
                    {
                        "knowledge.updated": knowledge_updated_handler(
                            manifest_repository,
                            LocalReviewedManifestSource(CORPUS, MANIFEST),
                            source_ref="data/knowledge/v2/corpus.json",
                        )
                    },
                )
                if local_knowledge
                else None
            )
            refund_consumer = (
                InboxConsumer(
                    InboxRepository(engine, max_retries=catalog.max_retries),
                    transport,
                    catalog,
                    {
                        "refund.confirmed": MockRefundReviewHandler(
                            MockRefundSnapshotAdapter(engine),
                            OutboxRepository(engine, catalog),
                        )
                    },
                )
                if settings is not None and settings.app_env != "production"
                else None
            )
            # 步骤28：业务事件的通知扇出（只写共享 Outbox）与通知发送消费者
            # （只写通知表）；均仅在开发 Mock 环境启用。
            followup_consumer = (
                InboxConsumer(
                    InboxRepository(engine, max_retries=catalog.max_retries),
                    transport,
                    catalog,
                    {
                        event_type: NotificationFanoutHandler(
                            OutboxRepository(engine, catalog)
                        )
                        for event_type in NotificationFanoutHandler.TEMPLATES
                    },
                )
                if settings is not None and settings.app_env != "production"
                else None
            )
            notification_consumer = (
                InboxConsumer(
                    InboxRepository(engine, max_retries=catalog.max_retries),
                    transport,
                    catalog,
                    {"notification.created": NotificationSendHandler(MockNotificationSender())},
                )
                if settings is not None and settings.app_env != "production"
                else None
            )
            task_dispatcher = TaskDispatcher(task_repository, registered_handlers)
            while not stopper.is_stopped():
                for owner_module in catalog.producers:
                    for tenant_id in repository.pending_tenants(owner_module):
                        publisher.publish_pending(owner_module, tenant_id, worker_id)
                if task_consumer is not None:
                    task_consumer.poll_once(catalog.entries["agent.task.created"]["queue"])
                if knowledge_consumer is not None:
                    knowledge_consumer.poll_once(catalog.entries["knowledge.updated"]["queue"])
                    assert settings is not None and index_jobs is not None
                    assert manifest_repository is not None and knowledge_publisher is not None
                    for tenant_id, backend in index_jobs.pending_for_release(local_release_id):
                        result = KnowledgeIndexWorker(
                            index_jobs,
                            manifest_repository,
                            {backend: LocalReviewedIndexBackend(settings, backend)},
                        ).run_once(tenant_id, local_release_id, backend, worker_id)
                        if result == "succeeded":
                            revision = knowledge_publisher.get_revision(tenant_id, local_release_id)
                            if revision is not None:
                                knowledge_publisher.activate_if_complete(
                                    tenant_id, local_release_id, revision
                                )
                if refund_consumer is not None:
                    refund_consumer.poll_once(catalog.entries["refund.confirmed"]["queue"])
                if followup_consumer is not None:
                    for event_type in NotificationFanoutHandler.TEMPLATES:
                        followup_consumer.poll_once(catalog.entries[event_type]["queue"])
                if notification_consumer is not None:
                    notification_consumer.poll_once(
                        catalog.entries["notification.created"]["queue"]
                    )
                for task_type in registered_handlers:
                    for tenant_id in task_repository.pending_tenants(task_type):
                        task_dispatcher.run_one(tenant_id, task_type, worker_id)
                time.sleep(MESSAGE_POLL_INTERVAL_S)
        except Exception:  # noqa: BLE001 - 断线后重建连接并保留 Outbox
            logger.exception("消息发布连接中断，稍后重连")
            time.sleep(2)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001 - 关闭失败不应阻断下一次重连
                    logger.exception("RabbitMQ 连接关闭失败")


def main() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    stopper = StopAndWait()
    stopper.register_signals()
    session_repository = build_session_repository(settings)
    chat_service = build_chat_service(
        settings,
        session_repository=session_repository,
        checkpointer=MySQLCheckpointSaver(engine),
        profile_repository=MySQLMemoryProfileRepository(engine),
        tool_invocations=MySQLToolInvocationRepository(engine),
    )
    auth_gateway = build_auth_gateway(settings)

    handlers = (
        {"agent.chat": build_chat_task_handler(chat_service, auth_gateway)}
        if auth_gateway.mock_login_enabled()
        else {}
    )
    memory_thread = threading.Thread(
        target=run_memory_cleanup,
        args=(stopper, MySQLMemoryProfileRepository(engine)),
        name="memory-maintenance",
    )
    try:
        memory_thread.start()
        run_messaging(stopper, engine, settings.rabbitmq_url, handlers, settings)
    finally:
        stopper.request_stop()
        if memory_thread.is_alive():
            memory_thread.join(timeout=3)
        session_repository.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
