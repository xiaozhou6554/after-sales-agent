"""单路检索观测：未命中与依赖故障分开，供后续双路RAG使用。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from application.knowledge.retrieval.ports import RetrievalHit, RetrievalUnavailable


@dataclass(frozen=True)
class BranchResult:
    source: str
    status: str  # success / empty / error
    hits: tuple[RetrievalHit, ...]
    duration_ms: float
    error_code: str | None = None


def run_search_branch(
    source: str,
    search: Callable[[], list[RetrievalHit]],
    clock: Callable[[], float] = time.perf_counter,
) -> BranchResult:
    started = clock()
    try:
        hits = tuple(search())
    except RetrievalUnavailable:
        return BranchResult(
            source, "error", (), max(0.0, (clock() - started) * 1000), "DEPENDENCY_UNAVAILABLE"
        )
    return BranchResult(
        source, "success" if hits else "empty", hits, max(0.0, (clock() - started) * 1000)
    )
