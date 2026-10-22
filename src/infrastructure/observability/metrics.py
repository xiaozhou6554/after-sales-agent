"""统一指标采集点（技术详细规格 20.2）：进程内采集，后续业务只补充业务事件。

预留点：采集器提供 incr/observe/snapshot 三个稳定方法；导出后端（如
Prometheus）在后续运维步骤接入，业务侧不各自创建一套日志或指标格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * len(ordered))) - 1))
    return ordered[index]


@dataclass
class MetricsSnapshot:
    counters: dict[str, float] = field(default_factory=dict)
    latencies: dict[str, dict] = field(default_factory=dict)


class InMemoryMetricsCollector:
    """按名称+标签维度累计计数与延迟观测；进程内实现，重启清零。"""

    def __init__(self, max_values_per_key: int = 10000) -> None:
        self._counters: dict[str, float] = {}
        self._values: dict[str, list[float]] = {}
        self._max_values = max_values_per_key

    @staticmethod
    def _key(name: str, tags: dict | None) -> str:
        if not tags:
            return name
        joined = ",".join(f"{k}={tags[k]}" for k in sorted(tags))
        return f"{name}|{joined}"

    def incr(self, name: str, value: float = 1.0, tags: dict | None = None) -> None:
        key = self._key(name, tags)
        self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value_ms: float, tags: dict | None = None) -> None:
        key = self._key(name, tags)
        values = self._values.setdefault(key, [])
        if len(values) < self._max_values:
            values.append(float(value_ms))

    def snapshot(self) -> MetricsSnapshot:
        latencies = {
            key: {
                "count": len(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "avg_ms": sum(values) / len(values) if values else None,
            }
            for key, values in self._values.items()
        }
        return MetricsSnapshot(counters=dict(self._counters), latencies=latencies)
