"""外部依赖熔断状态机；阈值统一取技术详细规格 1.4。"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

FAILURE_WINDOW_S = 30.0
FAILURE_COUNT = 5
RATE_SAMPLE_SIZE = 20
RATE_THRESHOLD = 0.5
OPEN_INTERVAL_S = 30.0
HALF_OPEN_PROBES = 3


class CircuitBreaker:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._history: deque[tuple[float, bool]] = deque(maxlen=RATE_SAMPLE_SIZE)
        self._state = "closed"
        self._opened_at = 0.0
        self._probe_successes = 0
        self._probe_inflight = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._state == "open":
                if now - self._opened_at < OPEN_INTERVAL_S:
                    return False
                self._state = "half_open"
                self._probe_successes = 0
                self._probe_inflight = 0
            if self._state == "half_open":
                if self._probe_successes + self._probe_inflight >= HALF_OPEN_PROBES:
                    return False
                self._probe_inflight += 1
            return True

    def record(self, *, failed: bool) -> None:
        with self._lock:
            now = self._clock()
            if self._state == "half_open":
                self._probe_inflight = max(0, self._probe_inflight - 1)
                if failed:
                    self._open(now)
                else:
                    self._probe_successes += 1
                    if self._probe_successes >= HALF_OPEN_PROBES:
                        self._state = "closed"
                        self._history.clear()
                return
            if self._state != "closed":
                return
            self._history.append((now, failed))
            while self._history and now - self._history[0][0] > FAILURE_WINDOW_S:
                self._history.popleft()
            failures = sum(item[1] for item in self._history)
            if failures >= FAILURE_COUNT or (
                len(self._history) >= RATE_SAMPLE_SIZE
                and failures / len(self._history) >= RATE_THRESHOLD
            ):
                self._open(now)

    def _open(self, now: float) -> None:
        self._state = "open"
        self._opened_at = now
        self._probe_inflight = 0
