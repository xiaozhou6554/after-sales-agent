"""独立 Cross-Encoder 重排端口；输出与候选顺序一一对应的原始分数。"""

from __future__ import annotations

from typing import Protocol, Sequence


class RerankUnavailable(RuntimeError):
    """本地模型缺失、校验失败或推理失败。"""


class RerankPort(Protocol):
    model_id: str
    model_revision: str

    def score(self, query: str, passages: Sequence[str]) -> Sequence[float]: ...
