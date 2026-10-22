"""开发环境按首次已激活发布请求装载本地候选RAG；绝不联网下载。"""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from application.knowledge.rag import RagRetriever
from application.knowledge.retrieval.ports import RetrievalUnavailable
from bootstrap.knowledge_rag_config import load_development_rag_config
from bootstrap.verify_knowledge_retrieval import PROJECT_ROOT, build_local_retrievers
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker


class LocalCandidateRagRetriever:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._lock = Lock()
        self._retriever: RagRetriever | None = None
        self._release_id: str | None = None

    def warm_up(self, tenant_id: str, knowledge_release_id: str) -> None:
        """在 MCP 标记就绪前装载本地模型并完成一次推理，避免首请求超时。"""
        self.search(
            "退货政策",
            tenant_id=tenant_id,
            knowledge_release_id=knowledge_release_id,
            top_k=1,
        )

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        knowledge_release_id: str,
        top_k: int | None = None,
    ):
        if self._retriever is None:
            with self._lock:
                if self._retriever is None:
                    try:
                        chunks = load_manifest_chunks(
                            PROJECT_ROOT / "data/knowledge/v2/corpus.json",
                            PROJECT_ROOT / "data/manifests/knowledge-v2.json",
                        )
                        if not chunks or chunks[0].release_id != knowledge_release_id:
                            raise RetrievalUnavailable("当前发布没有对应的本地知识索引")
                        _, vector, bm25 = build_local_retrievers(self._settings)
                        reranker = LocalCrossEncoderReranker(
                            Path(PROJECT_ROOT / "data/generated/models/bge-reranker-base")
                        )
                        self._retriever = RagRetriever(
                            vector, bm25, load_development_rag_config(), reranker=reranker
                        )
                        self._release_id = knowledge_release_id
                    except RetrievalUnavailable:
                        raise
                    except Exception as exc:
                        raise RetrievalUnavailable("本地RAG模型或索引不可用") from exc
        if self._release_id != knowledge_release_id:
            raise RetrievalUnavailable("本地RAG索引与当前发布不一致")
        return self._retriever.search(
            query,
            tenant_id=tenant_id,
            knowledge_release_id=knowledge_release_id,
            top_k=top_k,
        )
