"""就绪健康状态判定：区分存活、就绪与显式降级（技术详细规格 15、26）。

规则（技术详细规格 11/15）：
- 全部依赖可用 → ``ready``；
- 仅 Redis 不可用且开发/测试显式允许内存降级 → ``degraded``，不承诺持久化；
- 其余任何依赖故障一律失败关闭 → ``unavailable``。
生产环境禁止内存降级，该开关在配置层已被拒绝。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from application.common.ports.dependency_probe import DependencyProbe, ProbeResult

READY = "ready"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"

REDIS_DEP = "redis"


@dataclass(frozen=True)
class HealthReport:
    status: str
    dependencies: dict[str, str]


class ReadinessCheckUseCase:
    """聚合各依赖探测结果并输出三态健康状态，不包含任何用户业务数据。"""

    def __init__(self, probes: tuple[DependencyProbe, ...], memory_fallback_allowed: bool) -> None:
        self._probes = probes
        self._memory_fallback_allowed = memory_fallback_allowed

    def probe_all(self) -> list[ProbeResult]:
        """逐项探测并返回明细；供诊断工具与健康用例共用，不抛出异常。"""
        return [self._safe_probe(probe) for probe in self._probes]

    def execute(self) -> HealthReport:
        results = self.probe_all()
        dependencies = {result.name: ("up" if result.ok else "down") for result in results}
        all_up = all(result.ok for result in results)
        if all_up:
            status = READY
        else:
            redis_down = any(result.name == REDIS_DEP and not result.ok for result in results)
            only_redis_down = all(result.ok or result.name == REDIS_DEP for result in results)
            if redis_down and only_redis_down and self._memory_fallback_allowed:
                status = DEGRADED
            else:
                status = UNAVAILABLE
        return HealthReport(status=status, dependencies=dependencies)

    def _safe_probe(self, probe: DependencyProbe) -> ProbeResult:
        started = time.perf_counter()
        try:
            return probe.probe()
        except Exception as exc:  # 失败关闭：任何探测异常都按依赖故障处理。
            latency_ms = int((time.perf_counter() - started) * 1000)
            return ProbeResult(
                name=probe.name, ok=False, latency_ms=latency_ms, error=type(exc).__name__
            )
