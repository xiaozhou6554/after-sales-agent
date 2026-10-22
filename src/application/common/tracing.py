"""链路上下文传播（技术详细规格 20.1、8.4，纯标准库）。

一次请求串联 API→Agent→MCP：入口绑定 request_id/correlation_id/trace_id/
thread_id 与真实主体，跨层经 contextvars 读取；不在模型参数中传递，
也不把身份写入模型可见内容。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from application.common.context import TraceContext


@dataclass(frozen=True)
class SubjectFrame:
    """当前请求的真实操作者（服务端认证上下文摘要，不含 scope 明细）。"""

    tenant_id: str
    subject_id: str


_trace_var: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "after_sales_trace", default=None
)
_subject_var: contextvars.ContextVar[SubjectFrame | None] = contextvars.ContextVar(
    "after_sales_subject", default=None
)


def bind_request_context(
    trace: TraceContext, subject: SubjectFrame | None = None
) -> list[contextvars.Token]:
    """绑定当前请求的链路与主体；返回 token 列表供请求结束时复位。"""
    tokens = [_trace_var.set(trace)]
    if subject is not None:
        tokens.append(_subject_var.set(subject))
    return tokens


def reset_request_context(tokens: list[contextvars.Token]) -> None:
    for token in reversed(tokens):
        token.var.reset(token)


def current_trace() -> TraceContext | None:
    return _trace_var.get()


def current_subject() -> SubjectFrame | None:
    return _subject_var.get()
