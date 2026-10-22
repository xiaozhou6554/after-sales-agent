"""统一只读重试预算（《技术详细规格》1.4、9.3）。

- 只读瞬态失败最多自动重试 1 次：attempt_count 含首次最多 2，retry_count 不含首次最多 1；
- 仅依赖不可用/超时算瞬态；认证、参数、授权、业务规则与空结果一律不重试；
- 高风险写操作重试次数为 0，只能按幂等键查询状态；
- 退避 100ms + 随机抖动，且必须在剩余请求时限内执行；
- SDK、HTTP、MCP 客户端不得叠加隐式重试：预算是唯一重试入口。
"""

from __future__ import annotations

import random
from collections.abc import Callable

from application.common.errors import ErrorCode

READONLY_MAX_RETRIES = 1
RETRY_BACKOFF_BASE_MS = 100
RETRY_JITTER_MAX_MS = 50

# 仅这两类是瞬态失败；其余错误码重试只会重复同样结果。
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset(
    {ErrorCode.DEPENDENCY_UNAVAILABLE.value, ErrorCode.TIMEOUT.value}
)


class RetryBudgetExhausted(Exception):
    """预算耗尽：调用方不得再发起尝试，转人工或按错误返回。"""


class RetryBudget:
    """一次逻辑调用的重试预算；同一实例不得跨逻辑调用复用。"""

    def __init__(
        self,
        *,
        max_retries: int = READONLY_MAX_RETRIES,
        deadline_s: float | None = None,
        clock: Callable[[], float] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._deadline_s = deadline_s
        self._clock = clock or _monotonic
        self._jitter = jitter or random.random
        self._start = self._clock()
        self.attempt_count = 0
        self.retry_count = 0

    def begin_attempt(self) -> None:
        """记录一次尝试；超过"首次+重试"上限即拒绝（不突破预算）。"""
        if self.attempt_count > self._max_retries:
            raise RetryBudgetExhausted(
                f"attempt_count={self.attempt_count} 已超过 1+{self._max_retries} 预算"
            )
        self.attempt_count += 1

    def should_retry(self, error_code: str) -> bool:
        """判断刚失败的尝试是否允许重试；写操作与预算/时限不足一律不允许。"""
        if self.retry_count >= self._max_retries:
            return False
        if error_code not in TRANSIENT_ERROR_CODES:
            return False
        return self._remaining_s() > 0

    def record_retry(self) -> float:
        """登记一次重试并返回退避秒数（100ms + 抖动，不超剩余时限）。"""
        self.retry_count += 1
        delay_ms = RETRY_BACKOFF_BASE_MS + self._jitter() * RETRY_JITTER_MAX_MS
        remaining_s = self._remaining_s()
        return min(delay_ms / 1000, max(remaining_s, 0.0))

    def _remaining_s(self) -> float:
        if self._deadline_s is None:
            return float("inf")
        return self._deadline_s - (self._clock() - self._start)

    def remaining_s(self) -> float:
        """返回同一逻辑调用尚可使用的时间，供传输层限制单次尝试。"""
        return max(0.0, self._remaining_s())


def _monotonic() -> float:
    import time

    return time.monotonic()
