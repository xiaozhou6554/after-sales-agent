"""数据库任务调度与已提交任务事件的校验。"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.common.events import EventEnvelope
from application.common.tasks.models import TaskLeaseLostError, TaskRecord
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository

logger = logging.getLogger("after_sales.worker")


@dataclass(frozen=True)
class TaskOutcome:
    result_ref: str
    effect: Callable[[Connection, TaskRecord], None] | None = None


class TaskDeferred(RuntimeError):
    """外部结果未知，等待租约恢复后按原操作标识查询。"""


class TaskDispatcher:
    def __init__(
        self,
        repository: MySQLTaskRepository,
        handlers: dict[str, Callable[[TaskRecord], TaskOutcome]],
    ) -> None:
        self._repository = repository
        self._handlers = handlers

    def run_one(self, tenant_id: str, task_type: str, worker_id: str) -> bool:
        if task_type not in self._handlers:
            raise ValueError("任务类型没有受信任执行器")
        lease = self._repository.claim(tenant_id, task_type, worker_id)
        if lease is None:
            return False
        try:
            outcome = self._handlers[task_type](lease.task)
            self._repository.complete(lease, outcome.result_ref, outcome.effect)
        except TaskDeferred:
            return True
        except TaskLeaseLostError:
            raise
        except Exception:  # noqa: BLE001 - 业务执行失败持久化类型，不泄露原文
            logger.exception("后台任务执行失败: task_id=%s", lease.task.task_id)
            self._repository.fail(lease, "TASK_EXECUTION_FAILED")
        return True


def task_created_handler(connection: Connection, event: EventEnvelope) -> str:
    """只确认已持久化任务；实际任务由数据库租约调度，消息重投不重建任务。"""
    payload = event.payload_dict()
    task_id = payload["task_id"]
    row = (
        connection.execute(
            text("""SELECT actor_subject_id,customer_id FROM agent_task
            WHERE tenant_id=:tenant_id AND task_id=:task_id"""),
            {"tenant_id": event.tenant_id, "task_id": task_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError("任务事件缺少已提交的任务记录")
    if (
        row["actor_subject_id"] != payload["actor_subject_id"]
        or row["customer_id"] != payload["customer_id"]
    ):
        raise ValueError("任务事件的主体与客户不匹配")
    return task_id
