"""只读 KG 查询契约；作用域由可信调用方注入。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class GraphIntegrityError(RuntimeError):
    """图中存在越界、过期或无证据数据；不得当作空命中处理。"""


@dataclass(frozen=True)
class GraphScope:
    tenant_id: str
    knowledge_release_id: str
    # None 表示可信授权层授予该租户整批知识；空集合表示无实体权限。
    authorized_node_ids: frozenset[str] | None = None


@dataclass(frozen=True)
class GraphQuery:
    entity_names: tuple[str, ...]
    relation_types: tuple[str, ...]
    limit: int = 10
    as_of: date | None = None


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    label: str
    version: str


@dataclass(frozen=True)
class GraphRelation:
    from_id: str
    relation_type: str
    to_id: str
    version: str
    evidence_id: str


@dataclass(frozen=True)
class GraphEvidence:
    evidence_id: str
    document_id: str
    source: str
    version: str


@dataclass(frozen=True)
class GraphResult:
    knowledge_release_id: str
    nodes: tuple[GraphNode, ...]
    relations: tuple[GraphRelation, ...]
    evidence: tuple[GraphEvidence, ...]

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)


class GraphSearchPort(Protocol):
    def search(self, query: GraphQuery, scope: GraphScope) -> GraphResult: ...
