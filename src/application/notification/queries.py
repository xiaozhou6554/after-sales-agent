"""客户通知状态查询用例及读取端口。"""

from __future__ import annotations

from typing import Protocol

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext
from application.common.errors import ErrorCode

STATUS_LABELS = {
    "pending": "发送中",
    "sent": "已发送",
    "failed": "发送失败",
    "unknown": "待核实",
}


class NotificationReader(Protocol):
    def list_for_customer(
        self, tenant_id: str, customer_ids: tuple[str, ...], status: str | None, limit: int
    ) -> list[dict]: ...

    def get_for_customer(
        self, tenant_id: str, customer_ids: tuple[str, ...], notification_id: str
    ) -> dict | None: ...


class NotificationQueryUseCase:
    def __init__(self, reader: NotificationReader) -> None:
        self._reader = reader

    @staticmethod
    def _scope(auth: AuthContext) -> tuple[str, tuple[str, ...]]:
        if auth.subject_id is None or "customer" not in auth.roles:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "通知查询仅限客户本人")
        customer_ids = auth.data_scope.customer_ids
        if not customer_ids:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "通知查询缺少客户范围")
        return auth.tenant_id, customer_ids

    def list(self, auth: AuthContext, *, status: str | None, limit: int) -> dict:
        if status is not None and status not in STATUS_LABELS:
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "未知通知状态过滤条件")
        tenant_id, customer_ids = self._scope(auth)
        items = self._reader.list_for_customer(tenant_id, customer_ids, status, limit)
        for item in items:
            item["status_label"] = STATUS_LABELS.get(str(item["status"]), "待核实")
        unknown = sum(item["status"] == "unknown" for item in items)
        return {"notifications": items, "unknown_count": unknown}

    def get(self, auth: AuthContext, notification_id: str) -> dict:
        tenant_id, customer_ids = self._scope(auth)
        item = self._reader.get_for_customer(tenant_id, customer_ids, notification_id)
        if item is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "通知不存在")
        item["status_label"] = STATUS_LABELS.get(str(item["status"]), "待核实")
        return item
