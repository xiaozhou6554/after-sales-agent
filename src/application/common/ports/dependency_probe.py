"""基础依赖探测端口：由基础设施层适配器实现，应用层只依赖该抽象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProbeResult:
    """单个依赖的探测结果；错误信息只保留异常类型，不含连接凭据。"""

    name: str
    ok: bool
    latency_ms: int
    error: str | None = None


@runtime_checkable
class DependencyProbe(Protocol):
    """基础依赖就绪探测的最小协议；实现必须自行捕获异常，不向外抛出。"""

    name: str

    def probe(self) -> ProbeResult: ...
