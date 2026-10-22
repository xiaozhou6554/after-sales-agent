"""步骤09 统一只读重试预算单元测试（1.4：最多重试1次，100ms+抖动，时限内）。"""

from __future__ import annotations

import pytest

from application.common.execution.retry_policy import (
    READONLY_MAX_RETRIES,
    RETRY_BACKOFF_BASE_MS,
    RetryBudget,
    RetryBudgetExhausted,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_readonly_budget_allows_single_retry() -> None:
    clock = FakeClock()
    budget = RetryBudget(clock=clock, jitter=lambda: 0.0)
    assert READONLY_MAX_RETRIES == 1
    budget.begin_attempt()
    assert budget.should_retry("DEPENDENCY_UNAVAILABLE")
    delay = budget.record_retry()
    assert RETRY_BACKOFF_BASE_MS <= delay * 1000 <= 150
    budget.begin_attempt()
    assert not budget.should_retry("DEPENDENCY_UNAVAILABLE")


def test_total_attempts_capped_at_two() -> None:
    budget = RetryBudget(clock=FakeClock(), jitter=lambda: 0.0)
    budget.begin_attempt()
    budget.should_retry("DEPENDENCY_UNAVAILABLE")
    budget.record_retry()
    budget.begin_attempt()
    with pytest.raises(RetryBudgetExhausted):
        budget.begin_attempt()


def test_non_transient_errors_never_retry() -> None:
    budget = RetryBudget(clock=FakeClock(), jitter=lambda: 0.0)
    budget.begin_attempt()
    for code in (
        "UNAUTHENTICATED",
        "RESOURCE_UNAVAILABLE",
        "INVALID_ARGUMENT",
        "IDEMPOTENCY_CONFLICT",
        "VERSION_CONFLICT",
    ):
        assert not budget.should_retry(code), code


def test_retry_requires_remaining_deadline() -> None:
    clock = FakeClock()
    budget = RetryBudget(deadline_s=0.5, clock=clock, jitter=lambda: 0.0)
    budget.begin_attempt()
    clock.advance(0.6)
    assert not budget.should_retry("DEPENDENCY_UNAVAILABLE")


def test_backoff_never_exceeds_remaining_deadline() -> None:
    clock = FakeClock()
    budget = RetryBudget(deadline_s=0.02, clock=clock, jitter=lambda: 0.0)
    budget.begin_attempt()
    delay = budget.record_retry()
    assert delay <= 0.02


def test_write_access_budget_disallows_retry() -> None:
    """高风险写操作重试次数为 0（构建时由 access 决定 max_retries）。"""
    budget = RetryBudget(max_retries=0, clock=FakeClock(), jitter=lambda: 0.0)
    budget.begin_attempt()
    assert not budget.should_retry("DEPENDENCY_UNAVAILABLE")
