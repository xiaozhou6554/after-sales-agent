"""并行双路召回、版本化分数映射与有证据的融合排序。

映射/权重/阈值须由调用方从冻结开发集选取并注入；此模块不提供声称已校准的默认值。
没有重排器时，final_score 仅表示融合排序分数，不代表 Cross-Encoder 重排。
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from application.knowledge.rag.rerank import RerankPort, RerankUnavailable
from application.knowledge.retrieval.branch import BranchResult, run_search_branch
from application.knowledge.retrieval.ports import (
    KeywordSearchPort,
    KnowledgeChunk,
    VectorSearchPort,
)


@dataclass(frozen=True)
class CalibrationCurve:
    """固定锚点的单调分数映射；不按当前查询候选重新缩放。"""

    version: str
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if not self.version.strip() or len(self.points) < 2:
            raise ValueError("校准映射需要版本和至少两个锚点")
        for index, (raw, relevance) in enumerate(self.points):
            if not math.isfinite(raw) or not math.isfinite(relevance) or not 0 <= relevance <= 1:
                raise ValueError("校准锚点必须是有限数，相关性须在[0,1]内")
            if index and (
                raw <= self.points[index - 1][0] or relevance < self.points[index - 1][1]
            ):
                raise ValueError("校准锚点必须按原始分数严格递增、相关性非递减")

    def map(self, score: float) -> float:
        if not math.isfinite(score):
            raise ValueError("检索分数必须是有限数")
        if score <= self.points[0][0]:
            return self.points[0][1]
        for (low_x, low_y), (high_x, high_y) in zip(self.points, self.points[1:]):
            if score <= high_x:
                return low_y + (score - low_x) * (high_y - low_y) / (high_x - low_x)
        return self.points[-1][1]


@dataclass(frozen=True)
class RagConfig:
    """一次检索固定使用的参数版本；权重和阈值无质量声称。"""

    version: str
    vector_calibration: CalibrationCurve
    bm25_calibration: CalibrationCurve
    vector_weight: float
    bm25_weight: float
    vector_threshold: float
    bm25_threshold: float
    fused_threshold: float
    vector_top_k: int = 20
    bm25_top_k: int = 20
    result_top_k: int = 3
    rerank_calibration: CalibrationCurve | None = None
    rerank_threshold: float | None = None

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("RAG 参数版本不能为空")
        for weight in (self.vector_weight, self.bm25_weight):
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("两路融合权重必须是正有限数")
        for threshold in (self.vector_threshold, self.bm25_threshold, self.fused_threshold):
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ValueError("相关性阈值必须在[0,1]内")
        if min(self.vector_top_k, self.bm25_top_k, self.result_top_k) <= 0:
            raise ValueError("各路和结果 Top-K 必须大于零")
        if (self.rerank_calibration is None) != (self.rerank_threshold is None):
            raise ValueError("重排校准映射和阈值必须同时配置")
        if self.rerank_threshold is not None and (
            not math.isfinite(self.rerank_threshold) or not 0 <= self.rerank_threshold <= 1
        ):
            raise ValueError("重排相关性阈值必须在[0,1]内")


@dataclass(frozen=True)
class RagEvidence:
    chunk: KnowledgeChunk
    vector_score: float | None
    bm25_score: float | None
    vector_relevance: float | None
    bm25_relevance: float | None
    final_score: float
    retrieval_source: tuple[str, ...]
    rerank_score: float | None = None
    rerank_relevance: float | None = None

    @property
    def evidence_id(self) -> str:
        return self.chunk.evidence_id


@dataclass(frozen=True)
class RagResult:
    query: str
    tenant_id: str
    knowledge_release_id: str
    retrieval_status: str  # complete / empty / partial / degraded
    results: tuple[RagEvidence, ...]
    branches: tuple[BranchResult, BranchResult]
    config_version: str
    vector_calibration_version: str
    bm25_calibration_version: str
    ranking_method: str = "fused_score_only"
    rerank_status: str = "not_configured"
    conflicts: tuple[str, ...] = ()
    rerank_error_code: str | None = None
    rerank_model_id: str | None = None
    rerank_model_revision: str | None = None
    rerank_calibration_version: str | None = None

    @property
    def vector_branch(self) -> BranchResult:
        return self.branches[0]

    @property
    def bm25_branch(self) -> BranchResult:
        return self.branches[1]

    @property
    def vector_status(self) -> str:
        return _branch_status(self.vector_branch)

    @property
    def bm25_status(self) -> str:
        return _branch_status(self.bm25_branch)

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(result.evidence_id for result in self.results))

    @property
    def accepted_results(self) -> tuple[RagEvidence, ...]:
        """仅经过 Cross-Encoder 阈值判定的证据可交给生成节点。"""
        return self.results if self.rerank_status == "success" else ()

    @property
    def generation_ready(self) -> bool:
        return bool(self.accepted_results)


def _branch_status(branch: BranchResult) -> str:
    return "failed" if branch.status == "error" else branch.status


def _checked_branch(branch: BranchResult, tenant_id: str, release_id: str) -> BranchResult:
    if branch.status == "error":
        return branch
    for hit in branch.hits:
        chunk = hit.chunk
        if (
            hit.source != branch.source
            or chunk.tenant_id != tenant_id
            or chunk.release_id != release_id
            or chunk.status != "published"
            or not chunk.chunk_id
            or not chunk.evidence_id
            or not math.isfinite(hit.score)
        ):
            return BranchResult(
                branch.source, "error", (), branch.duration_ms, "INVALID_RETRIEVAL_RESULT"
            )
    return branch


def _run_branch(source: str, search) -> BranchResult:
    started = time.perf_counter()
    try:
        return run_search_branch(source, search)
    except Exception:
        # Step17 只捕获 RetrievalUnavailable；其他适配器异常也不得掩盖另一支路结果。
        return BranchResult(
            source,
            "error",
            (),
            max(0.0, (time.perf_counter() - started) * 1000),
            "RETRIEVAL_ERROR",
        )


def _identity(chunk: KnowledgeChunk) -> tuple[str, str, str]:
    return chunk.tenant_id, chunk.release_id, chunk.chunk_id


def _same_evidence(left: KnowledgeChunk, right: KnowledgeChunk) -> bool:
    return (
        left.document_id == right.document_id
        and left.evidence_id == right.evidence_id
        and left.version == right.version
        and left.title == right.title
        and left.content == right.content
    )


class RagRetriever:
    """Step18 核心：注入两个 Step17 端口和一个显式参数版本。"""

    def __init__(
        self,
        vector_search: VectorSearchPort,
        keyword_search: KeywordSearchPort,
        config: RagConfig,
        *,
        reranker: RerankPort | None = None,
    ) -> None:
        if reranker is not None and config.rerank_calibration is None:
            raise ValueError("已配置重排器时必须提供版本化重排映射与阈值")
        self._vector = vector_search
        self._keyword = keyword_search
        self._config = config
        self._reranker = reranker

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        knowledge_release_id: str,
        top_k: int | None = None,
    ) -> RagResult:
        if not query.strip() or not tenant_id.strip() or not knowledge_release_id.strip():
            raise ValueError("查询、可信租户和知识发布版本不能为空")
        limit = self._config.result_top_k if top_k is None else top_k
        if not 1 <= limit <= self._config.result_top_k:
            raise ValueError("top_k 必须在 1 到配置的结果上限之间")

        # 两支路同一租户、发布版本并发执行；上游负责统一超时/重试预算。
        with ThreadPoolExecutor(max_workers=2) as executor:
            vector_future = executor.submit(
                _run_branch,
                "vector",
                lambda: self._vector.search(
                    query, tenant_id, knowledge_release_id, self._config.vector_top_k
                ),
            )
            bm25_future = executor.submit(
                _run_branch,
                "bm25",
                lambda: self._keyword.search(
                    query, tenant_id, knowledge_release_id, self._config.bm25_top_k
                ),
            )
            vector = _checked_branch(vector_future.result(), tenant_id, knowledge_release_id)
            bm25 = _checked_branch(bm25_future.result(), tenant_id, knowledge_release_id)
        branches = (vector, bm25)

        chunks: dict[tuple[str, str, str], KnowledgeChunk] = {}
        scores: dict[tuple[str, str, str], dict[str, tuple[float, float]]] = {}
        conflicts: set[str] = set()
        for branch in branches:
            if branch.status == "error":
                continue
            curve = (
                self._config.vector_calibration
                if branch.source == "vector"
                else self._config.bm25_calibration
            )
            threshold = (
                self._config.vector_threshold
                if branch.source == "vector"
                else self._config.bm25_threshold
            )
            for hit in branch.hits:
                key = _identity(hit.chunk)
                previous = chunks.get(key)
                if previous is not None and not _same_evidence(previous, hit.chunk):
                    conflicts.add(hit.chunk.chunk_id)
                    continue
                chunks[key] = hit.chunk
                relevance = curve.map(hit.score)
                if relevance < threshold:
                    continue
                route_scores = scores.setdefault(key, {})
                if branch.source not in route_scores or relevance > route_scores[branch.source][1]:
                    route_scores[branch.source] = (hit.score, relevance)

        rerank_status = "not_configured" if self._reranker is None else "skipped_empty"
        ranking_method = "fused_score_only"
        rerank_error_code = None
        # 同一 chunk 的证据元数据矛盾时，不能把任何候选当作可靠结果。
        if conflicts:
            results: tuple[RagEvidence, ...] = ()
            status = "degraded"
            if self._reranker is not None:
                rerank_status = "skipped_conflict"
        else:
            ranked: list[RagEvidence] = []
            for key, route_scores in scores.items():
                weights = {
                    "vector": self._config.vector_weight,
                    "bm25": self._config.bm25_weight,
                }
                weight_sum = sum(weights[source] for source in route_scores)
                fused = sum(
                    weights[source] * relevance / weight_sum
                    for source, (_, relevance) in route_scores.items()
                )
                if fused < self._config.fused_threshold:
                    continue
                ranked.append(
                    RagEvidence(
                        chunk=chunks[key],
                        vector_score=route_scores.get("vector", (None, None))[0],
                        bm25_score=route_scores.get("bm25", (None, None))[0],
                        vector_relevance=route_scores.get("vector", (None, None))[1],
                        bm25_relevance=route_scores.get("bm25", (None, None))[1],
                        final_score=fused,
                        retrieval_source=tuple(
                            source for source in ("vector", "bm25") if source in route_scores
                        ),
                    )
                )
            ranked.sort(key=lambda item: (-item.final_score, item.chunk.chunk_id))
            failures = sum(branch.status == "error" for branch in branches)
            if ranked and self._reranker is not None:
                try:
                    passages = tuple(item.chunk.content for item in ranked) + tuple(
                        item.chunk.search_text for item in ranked
                    )
                    view_scores = tuple(self._reranker.score(query, passages))
                    if len(view_scores) != 2 * len(ranked) or any(
                        not isinstance(score, (int, float)) or not math.isfinite(score)
                        for score in view_scores
                    ):
                        raise ValueError("重排分数数量或数值无效")
                    raw_scores = tuple(
                        max(view_scores[index], view_scores[index + len(ranked)])
                        for index in range(len(ranked))
                    )
                    curve = self._config.rerank_calibration
                    threshold = self._config.rerank_threshold
                    assert curve is not None and threshold is not None
                    accepted = []
                    for item, raw_score in zip(ranked, raw_scores):
                        relevance = curve.map(float(raw_score))
                        if relevance >= threshold:
                            accepted.append(
                                replace(
                                    item,
                                    rerank_score=float(raw_score),
                                    rerank_relevance=relevance,
                                )
                            )
                    accepted.sort(
                        key=lambda item: (
                            -item.rerank_relevance,
                            -item.final_score,
                            item.chunk.chunk_id,
                        )
                    )
                    results = tuple(accepted[:limit])
                    rerank_status = "success"
                    ranking_method = "cross_encoder"
                except RerankUnavailable:
                    results = ()
                    rerank_status = "error"
                    rerank_error_code = "RERANK_UNAVAILABLE"
                except Exception:
                    results = ()
                    rerank_status = "error"
                    rerank_error_code = "RERANK_ERROR"
            else:
                # 兼容旧调用者可观测初排候选，但未配置重排器时不得交给生成节点。
                results = tuple(ranked[:limit])
            status = (
                "degraded"
                if rerank_status == "error"
                else "partial"
                if failures and results
                else "degraded"
                if failures
                else "complete"
                if results
                else "empty"
            )

        return RagResult(
            query=query,
            tenant_id=tenant_id,
            knowledge_release_id=knowledge_release_id,
            retrieval_status=status,
            results=results,
            branches=branches,
            config_version=self._config.version,
            vector_calibration_version=self._config.vector_calibration.version,
            bm25_calibration_version=self._config.bm25_calibration.version,
            ranking_method=ranking_method,
            rerank_status=rerank_status,
            conflicts=tuple(sorted(conflicts)),
            rerank_error_code=rerank_error_code,
            rerank_model_id=self._reranker.model_id if self._reranker else None,
            rerank_model_revision=self._reranker.model_revision if self._reranker else None,
            rerank_calibration_version=(
                self._config.rerank_calibration.version
                if self._config.rerank_calibration is not None
                else None
            ),
        )
