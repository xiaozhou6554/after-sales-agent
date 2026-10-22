"""纯判定：只有同一清单的向量、BM25、图谱都经验证成功才可激活。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BACKENDS = frozenset({"vector", "bm25", "graph"})


@dataclass(frozen=True)
class IndexJobState:
    backend: str
    knowledge_release_id: str
    manifest_digest: str
    status: str
    revision: int


@dataclass(frozen=True)
class PublicationDecision:
    status: Literal["pending", "indexing_failed", "ready"]
    reason: str | None = None


def decide_publication(
    knowledge_release_id: str,
    manifest_digest: str,
    jobs: tuple[IndexJobState, ...],
) -> PublicationDecision:
    """拒绝缺路、重复路、错发布、错清单和未完成任务。"""
    if not knowledge_release_id or not manifest_digest:
        raise ValueError("发布版本与清单摘要不能为空")
    names = [job.backend for job in jobs]
    if len(names) != len(set(names)) or not set(names).issubset(BACKENDS):
        return PublicationDecision("indexing_failed", "索引任务后端重复或未知")
    if any(
        job.knowledge_release_id != knowledge_release_id or job.manifest_digest != manifest_digest
        for job in jobs
    ):
        return PublicationDecision("indexing_failed", "索引任务版本或清单摘要不一致")
    if any(job.status == "failed" for job in jobs):
        return PublicationDecision("indexing_failed", "索引任务失败")
    if set(names) != BACKENDS:
        return PublicationDecision("pending")
    if any(job.status != "succeeded" for job in jobs):
        return PublicationDecision("pending")
    if any(job.revision < 1 for job in jobs):
        return PublicationDecision("indexing_failed", "成功任务缺少持久化修订号")
    return PublicationDecision("ready")
