"""Chroma 开发向量支路；Milvus 生产边界共享同一个 VectorSearchPort。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import chromadb
import numpy as np

from application.knowledge.retrieval.ports import (
    EmbeddingPort,
    KnowledgeChunk,
    RetrievalHit,
    RetrievalUnavailable,
)


def _normalized_vectors(
    vectors: list[list[float]], expected: int, dimension: int
) -> list[list[float]]:
    if len(vectors) != expected:
        raise ValueError("Embedding 返回数量与输入切片不一致")
    normalized = []
    for vector in vectors:
        values = np.asarray(vector, dtype=np.float32)
        if values.shape != (dimension,) or not np.all(np.isfinite(values)):
            raise ValueError("Embedding 维度或数值无效")
        norm = float(np.linalg.norm(values))
        if norm <= 0:
            raise ValueError("Embedding 不能是零向量")
        normalized.append((values / norm).tolist())
    return normalized


class ChromaVectorAdapter:
    @classmethod
    def persistent(
        cls,
        path: Path,
        embedder: EmbeddingPort,
        dimension: int,
        *,
        hnsw_m: int,
        ef_construction: int,
        ef_search: int,
    ) -> "ChromaVectorAdapter":
        return cls(
            chromadb.PersistentClient(path=str(path)),
            embedder,
            dimension,
            hnsw_m=hnsw_m,
            ef_construction=ef_construction,
            ef_search=ef_search,
        )

    def __init__(
        self,
        client: chromadb.ClientAPI,
        embedder: EmbeddingPort,
        dimension: int,
        *,
        hnsw_m: int,
        ef_construction: int,
        ef_search: int,
    ) -> None:
        if dimension != 512:
            raise ValueError("向量维度必须与规格512一致")
        self._client = client
        self._embedder = embedder
        self._dimension = dimension
        self._hnsw_m = hnsw_m
        self._ef_construction = ef_construction
        self._ef_search = ef_search
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._collection = None

    def build(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or len({chunk.release_id for chunk in chunks}) != 1:
            raise ValueError("向量索引必须基于同一非空发布版本")
        release_id = chunks[0].release_id
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("向量切片标识重复")
        name = "knowledge_" + hashlib.sha256(release_id.encode()).hexdigest()[:24]
        manifest_digest = hashlib.sha256(
            json.dumps(
                sorted(
                    (chunk.tenant_id, chunk.chunk_id, chunk.content, chunk.status)
                    for chunk in chunks
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        vectors = _normalized_vectors(
            self._embedder.encode([chunk.search_text for chunk in chunks]),
            len(chunks),
            self._dimension,
        )
        metadata = {
            "hnsw:space": "cosine",
            "hnsw:M": self._hnsw_m,
            "hnsw:construction_ef": self._ef_construction,
            "hnsw:search_ef": self._ef_search,
            "release_id": release_id,
            "model_id": self._embedder.model_id,
            "dimension": self._dimension,
            "manifest_digest": manifest_digest,
        }
        try:
            collection = self._client.get_or_create_collection(name=name, metadata=metadata)
            actual = collection.metadata or {}
            if any(actual.get(key) != value for key, value in metadata.items()):
                raise ValueError("向量集合模型、距离、维度或发布版本不匹配")
            existing = set(collection.get(include=[])["ids"])
            requested = {chunk.chunk_id for chunk in chunks}
            if existing and existing != requested:
                raise ValueError("既有向量集合切片与本次清单不一致")
            collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=vectors,
                documents=[chunk.content for chunk in chunks],
                metadatas=[
                    {
                        "tenant_id": chunk.tenant_id,
                        "release_id": release_id,
                        "document_id": chunk.document_id,
                        "evidence_id": chunk.evidence_id,
                        "version": chunk.version,
                        "status": chunk.status,
                    }
                    for chunk in chunks
                ],
            )
        except ValueError:
            raise
        except Exception as exc:
            raise RetrievalUnavailable("Chroma 向量索引构建失败") from exc
        self._collection = collection
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}

    def search(self, query: str, tenant_id: str, release_id: str, top_k: int) -> list[RetrievalHit]:
        if self._collection is None:
            raise RetrievalUnavailable("Chroma 向量索引尚未构建")
        if top_k <= 0:
            raise ValueError("top_k 必须大于零")
        if self._collection.metadata.get("release_id") != release_id:
            raise ValueError("查询发布版本与向量索引不一致")
        vector = _normalized_vectors(self._embedder.encode([query]), 1, self._dimension)[0]
        try:
            result = self._collection.query(
                query_embeddings=[vector],
                n_results=top_k,
                where={
                    "$and": [
                        {"tenant_id": tenant_id},
                        {"release_id": release_id},
                        {"status": "published"},
                    ]
                },
                include=["distances"],
            )
        except Exception as exc:
            raise RetrievalUnavailable("Chroma 向量查询失败") from exc
        ids = result["ids"][0]
        distances = result["distances"][0]
        return [
            RetrievalHit(
                self._chunks[chunk_id], max(-1.0, min(1.0, 1.0 - float(distance))), "vector"
            )
            for chunk_id, distance in zip(ids, distances)
        ]


class MilvusVectorAdapter:
    """生产端口占位：未连接真实 Milvus 时明确失败，绝不伪造检索命中。"""

    def build(self, chunks: list[KnowledgeChunk]) -> None:
        raise RetrievalUnavailable("生产 Milvus 适配器尚未配置")

    def search(self, query: str, tenant_id: str, release_id: str, top_k: int) -> list[RetrievalHit]:
        raise RetrievalUnavailable("生产 Milvus 适配器尚未配置")
