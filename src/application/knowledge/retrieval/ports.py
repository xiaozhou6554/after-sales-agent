"""知识召回端口；空命中与依赖故障分别为返回空列表和异常。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RetrievalUnavailable(RuntimeError):
    """索引/模型不可用，不得伪装为零命中。"""


@dataclass(frozen=True)
class KnowledgeChunk:
    tenant_id: str
    release_id: str
    chunk_id: str
    document_id: str
    evidence_id: str
    version: int
    title: str
    content: str
    search_text: str
    status: str


@dataclass(frozen=True)
class RetrievalHit:
    chunk: KnowledgeChunk
    score: float
    source: str


class EmbeddingPort(Protocol):
    model_id: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class VectorSearchPort(Protocol):
    def build(self, chunks: list[KnowledgeChunk]) -> None: ...

    def search(
        self, query: str, tenant_id: str, release_id: str, top_k: int
    ) -> list[RetrievalHit]: ...


class KeywordSearchPort(Protocol):
    def build(self, chunks: list[KnowledgeChunk]) -> None: ...

    def search(
        self, query: str, tenant_id: str, release_id: str, top_k: int
    ) -> list[RetrievalHit]: ...
