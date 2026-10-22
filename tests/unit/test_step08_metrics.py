"""步骤08 指标采集点单元测试：计数、延迟分位与标签维度。"""

from __future__ import annotations

from infrastructure.observability.metrics import InMemoryMetricsCollector


def test_counters_accumulate_with_tags() -> None:
    collector = InMemoryMetricsCollector()
    collector.incr("http_requests", tags={"method": "GET", "path": "/me"})
    collector.incr("http_requests", tags={"method": "GET", "path": "/me"})
    collector.incr("http_requests", tags={"method": "POST", "path": "/auth/mock/session"})
    snapshot = collector.snapshot()
    assert snapshot.counters["http_requests|method=GET,path=/me"] == 2
    assert snapshot.counters["http_requests|method=POST,path=/auth/mock/session"] == 1


def test_latency_percentiles() -> None:
    collector = InMemoryMetricsCollector()
    for ms in range(1, 101):
        collector.observe("http_latency_ms", ms, tags={"path": "/me"})
    stats = collector.snapshot().latencies["http_latency_ms|path=/me"]
    assert stats["count"] == 100
    assert stats["p50_ms"] == 50
    assert stats["p95_ms"] == 95
    assert abs(stats["avg_ms"] - 50.5) < 0.01


def test_empty_metrics_snapshot_is_honest_zero() -> None:
    collector = InMemoryMetricsCollector()
    snapshot = collector.snapshot()
    assert snapshot.counters == {}
    assert snapshot.latencies == {}
