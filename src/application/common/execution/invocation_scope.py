"""工具调用的可信租户作用域，不进入模型或传输参数。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_tenant_id: ContextVar[str | None] = ContextVar("tool_tenant_id", default=None)


@contextmanager
def bind_tool_tenant(tenant_id: str) -> Iterator[None]:
    token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


def current_tool_tenant() -> str | None:
    return _tenant_id.get()


class ToolInvocationPending(RuntimeError):
    """工具调用仍被租约持有或外部写结果未知，必须等待恢复。"""
