"""单次请求的绝对时限，供编排和依赖调用共享剩余预算。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


@contextmanager
def bind_deadline_at(deadline_monotonic: float) -> Iterator[None]:
    token = _deadline.set(deadline_monotonic)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds() -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())
