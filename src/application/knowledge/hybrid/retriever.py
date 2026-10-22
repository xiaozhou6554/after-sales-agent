"""并行运行完整RAG与KG；不可比较的分数不跨路相加。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from application.knowledge.graph.ports import (
    GraphIntegrityError,
    GraphQuery,
    GraphResult,
    GraphScope,
    GraphSearchPort,
)
from application.knowledge.rag.retriever import RagResult, RagRetriever


@dataclass(frozen=True)
class HybridResult:
    knowledge_release_id: str
    retrieval_status: str  # complete / partial / degraded / empty
    conflict_status: str  # none / indexing_failed
    rag: RagResult | None
    graph: GraphResult | None
    rag_status: str
    kg_status: str
    conflicts: tuple[str, ...] = ()

    @property
    def generation_ready(self) -> bool:
        return self.conflict_status == "none" and bool(
            (self.rag and self.rag.accepted_results) or (self.graph and self.graph.relations)
        )


class HybridRetriever:
    def __init__(self, rag: RagRetriever, graph: GraphSearchPort) -> None:
        self._rag = rag
        self._graph = graph

    def search(
        self,
        query: str,
        graph_query: GraphQuery,
        *,
        tenant_id: str,
        knowledge_release_id: str,
    ) -> HybridResult:
        if not tenant_id or not knowledge_release_id or not query.strip():
            raise ValueError("混合检索缺少可信租户、发布或问题")
        # 输入校验（尤其关系白名单）由GraphQuery/GraphSearchPort负责；实际执行固定一个发布。
        scope = GraphScope(tenant_id, knowledge_release_id)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid-knowledge") as pool:
            rag_future = pool.submit(
                self._rag.search,
                query,
                tenant_id=tenant_id,
                knowledge_release_id=knowledge_release_id,
            )
            graph_future = pool.submit(self._graph.search, graph_query, scope)
            rag_error = graph_error = None
            try:
                rag = rag_future.result()
            except Exception as exc:
                rag = None
                rag_error = exc
            try:
                graph = graph_future.result()
            except Exception as exc:
                graph = None
                graph_error = exc

        # 参数错误不能降级成“关系未命中”，由MCP边界返回INVALID_ARGUMENT。
        if isinstance(graph_error, ValueError):
            raise graph_error
        if isinstance(rag_error, ValueError):
            raise rag_error

        conflicts: set[str] = set()
        if isinstance(graph_error, GraphIntegrityError):
            conflicts.add("kg_integrity_error")
        if rag is not None and rag.knowledge_release_id != knowledge_release_id:
            conflicts.add("rag_release_mismatch")
        if graph is not None and graph.knowledge_release_id != knowledge_release_id:
            conflicts.add("kg_release_mismatch")
        if rag is not None:
            conflicts.update(rag.conflicts)
        if rag is not None and graph is not None:
            rag_by_evidence = {
                item.evidence_id: (item.chunk.document_id, str(item.chunk.version))
                for item in rag.accepted_results
            }
            for item in graph.evidence:
                prior = rag_by_evidence.get(item.evidence_id)
                if prior is not None and prior != (item.document_id, item.version):
                    conflicts.add(item.evidence_id)
        if conflicts:
            return HybridResult(
                knowledge_release_id,
                "degraded",
                "indexing_failed",
                None,
                None,
                rag.retrieval_status if rag else "error",
                "success" if graph and graph.relations else "empty" if graph else "error",
                tuple(sorted(conflicts)),
            )

        rag_status = rag.retrieval_status if rag else "error"
        kg_status = "success" if graph and graph.relations else "empty" if graph else "error"
        rag_ready = bool(rag and rag.accepted_results)
        kg_ready = bool(graph and graph.relations)
        failed = (
            rag is None
            or graph is None
            or rag_status in ("degraded", "partial")
            or (rag is not None and rag.results and rag.rerank_status != "success")
        )
        status = (
            "partial"
            if failed and (rag_ready or kg_ready)
            else "degraded"
            if failed
            else "complete"
            if rag_ready or kg_ready
            else "empty"
        )
        return HybridResult(
            knowledge_release_id,
            status,
            "none",
            rag,
            graph,
            rag_status,
            kg_status,
        )
