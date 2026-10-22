"""独立 BM25 召回；索引与向量支路共用 manifest 切片。"""

from __future__ import annotations

import re

import jieba
from rank_bm25 import BM25Okapi

from application.knowledge.retrieval.ports import KnowledgeChunk, RetrievalHit, RetrievalUnavailable

ASCII_TOKEN = re.compile(r"[a-zA-Z]+(?:[-_][a-zA-Z0-9]+)*[-_]?\d*|\d+(?:\.\d+)?")


def tokenize(text: str) -> list[str]:
    """保留型号/故障码整体，并对中文分词；查询与建索引用同一规则。"""
    lowered = text.casefold()
    ascii_tokens = ASCII_TOKEN.findall(lowered)
    cjk_text = re.sub(r"[a-zA-Z0-9_.-]+", " ", lowered)
    chinese = [token.strip() for token in jieba.cut(cjk_text) if token.strip()]
    return ascii_tokens + chinese


class BM25Adapter:
    def __init__(self) -> None:
        self._chunks: list[KnowledgeChunk] | None = None
        self._index: BM25Okapi | None = None
        self._release_id: str | None = None

    def build(self, chunks: list[KnowledgeChunk]) -> None:
        ids = [(chunk.tenant_id, chunk.release_id, chunk.chunk_id) for chunk in chunks]
        if not chunks or len(ids) != len(set(ids)):
            raise ValueError("BM25 切片为空或标识重复")
        if len({chunk.release_id for chunk in chunks}) != 1:
            raise ValueError("BM25 索引必须基于同一发布版本")
        self._chunks = list(chunks)
        self._release_id = chunks[0].release_id
        self._index = BM25Okapi([tokenize(chunk.search_text) for chunk in chunks])

    def search(self, query: str, tenant_id: str, release_id: str, top_k: int) -> list[RetrievalHit]:
        if self._index is None or self._chunks is None:
            raise RetrievalUnavailable("BM25 索引尚未构建")
        if top_k <= 0:
            raise ValueError("top_k 必须大于零")
        if release_id != self._release_id:
            raise ValueError("查询发布版本与BM25索引不一致")
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._index.get_scores(tokens)
        hits = [
            RetrievalHit(chunk, float(score), "bm25")
            for chunk, score in zip(self._chunks, scores)
            if chunk.tenant_id == tenant_id
            and chunk.release_id == release_id
            and chunk.status == "published"
            and score > 0
        ]
        return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.chunk_id))[:top_k]
