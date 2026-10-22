"""Authorized, cursor-paginated employee read models."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

from application.common.authorization import (
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode


@dataclass(frozen=True)
class WorkbenchPage:
    items: list[dict]
    next_offset: int | None


class WorkbenchReadPort(Protocol):
    def customers(
        self, tenant_id: str, customer_ids: tuple[str, ...], query: str, offset: int, limit: int
    ) -> WorkbenchPage: ...

    def tickets(
        self, tenant_id: str, customer_ids: tuple[str, ...], filters: dict, offset: int, limit: int
    ) -> WorkbenchPage: ...

    def handoffs(
        self,
        tenant_id: str,
        customer_ids: tuple[str, ...],
        status: str | None,
        offset: int,
        limit: int,
    ) -> WorkbenchPage: ...

    def refunds(
        self,
        tenant_id: str,
        customer_ids: tuple[str, ...],
        status: str | None,
        customer_id: str | None,
        offset: int,
        limit: int,
    ) -> WorkbenchPage: ...

    def ticket_detail(self, tenant_id: str, ticket_id: str) -> dict | None: ...


def _encode_cursor(auth: AuthContext, scope: str, offset: int) -> str:
    body = json.dumps(
        {"scope": scope, "offset": offset}, sort_keys=True, separators=(",", ":")
    ).encode()
    signature = hmac.new(auth.session_id.encode(), body, hashlib.sha256).digest()
    return ".".join(
        base64.urlsafe_b64encode(value).decode().rstrip("=") for value in (body, signature)
    )


def _decode_cursor(auth: AuthContext, scope: str, cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        body_encoded, signature_encoded = cursor.split(".", 1)
        body = base64.urlsafe_b64decode(body_encoded + "=" * (-len(body_encoded) % 4))
        signature = base64.urlsafe_b64decode(
            signature_encoded + "=" * (-len(signature_encoded) % 4)
        )
        expected = hmac.new(auth.session_id.encode(), body, hashlib.sha256).digest()
        value = json.loads(body)
        if not hmac.compare_digest(signature, expected) or value != {
            "scope": scope,
            "offset": value["offset"],
        }:
            raise ValueError
        offset = value["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "分页游标无效或已过期") from exc


def _customer_ids(auth: AuthContext) -> tuple[str, ...]:
    ids = tuple(item for item in auth.data_scope.customer_ids if item != "*")
    return ids


class WorkbenchQueries:
    def __init__(self, reads: WorkbenchReadPort) -> None:
        self._reads = reads

    @staticmethod
    def _require_employee(auth: AuthContext) -> None:
        if not set(auth.roles) & {"support", "human_agent", "supervisor"}:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")

    @staticmethod
    def _page(auth: AuthContext, scope: str, limit: int, cursor: str | None, fetch) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "分页数量必须为 1～100")
        offset = _decode_cursor(auth, scope, cursor)
        page = fetch(offset, limit + 1)
        items = page.items[:limit]
        next_offset = offset + limit if len(page.items) > limit else None
        return {
            "items": items,
            "next_cursor": _encode_cursor(auth, scope, next_offset)
            if next_offset is not None
            else None,
        }

    def customers(
        self, auth: AuthContext, *, query: str = "", cursor: str | None = None, limit: int = 20
    ) -> dict:
        self._require_employee(auth)
        verify_tool_context(auth, ("customer:read",))
        customer_ids = _customer_ids(auth)
        scope = json.dumps(["customers", query, customer_ids], separators=(",", ":"))
        return self._page(
            auth,
            scope,
            limit,
            cursor,
            lambda offset, size: self._reads.customers(
                auth.tenant_id, customer_ids, query, offset, size
            ),
        )

    def tickets(
        self,
        auth: AuthContext,
        *,
        customer_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        assigned_to: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        self._require_employee(auth)
        verify_tool_context(auth, ("ticket:read",))
        ids = _customer_ids(auth)
        if customer_id is not None and customer_id not in ids:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        filters = {
            "customer_id": customer_id,
            "status": status,
            "priority": priority,
            "assigned_to": assigned_to,
        }
        scope = json.dumps(["tickets", filters, ids], sort_keys=True, separators=(",", ":"))
        return self._page(
            auth,
            scope,
            limit,
            cursor,
            lambda offset, size: self._reads.tickets(auth.tenant_id, ids, filters, offset, size),
        )

    def ticket_detail(self, auth: AuthContext, ticket_id: str) -> dict:
        self._require_employee(auth)
        verify_tool_context(auth, ("ticket:read",))
        result = self._reads.ticket_detail(auth.tenant_id, ticket_id)
        if result is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        require_customer_access(auth, result["customer_id"], tenant_id=auth.tenant_id)
        return result

    def handoffs(
        self,
        auth: AuthContext,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        self._require_employee(auth)
        verify_tool_context(auth, ("handoff:read",))
        ids = _customer_ids(auth)
        scope = json.dumps(["handoffs", status, ids], separators=(",", ":"))
        return self._page(
            auth,
            scope,
            limit,
            cursor,
            lambda offset, size: self._reads.handoffs(auth.tenant_id, ids, status, offset, size),
        )

    def refunds(
        self,
        auth: AuthContext,
        *,
        status: str | None = None,
        customer_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        self._require_employee(auth)
        verify_tool_context(auth, ("refund:review",))
        if not set(auth.roles) & {"supervisor", "support", "human_agent"}:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        ids = _customer_ids(auth)
        if customer_id is not None and customer_id not in ids:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "资源不存在或不在授权范围内")
        scope = json.dumps(["refunds", status, customer_id, ids], separators=(",", ":"))
        return self._page(
            auth,
            scope,
            limit,
            cursor,
            lambda offset, size: self._reads.refunds(
                auth.tenant_id, ids, status, customer_id, offset, size
            ),
        )
