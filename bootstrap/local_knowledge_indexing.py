"""仅开发环境：用已有本地资源构建三路索引并逐路读回核验。"""

from __future__ import annotations

import hashlib

from application.knowledge.indexing.models import KnowledgeIndexLease
from bootstrap.settings import RuntimeSettings
from bootstrap.verify_knowledge_retrieval import PROJECT_ROOT, build_local_retrievers
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.graph.seed import build_seed_plan, seed_reviewed_graph
from infrastructure.persistence.neo4j.connection import create_neo4j_driver
from infrastructure.retrieval.bm25_adapter import BM25Adapter

CORPUS = PROJECT_ROOT / "data/knowledge/v2/corpus.json"
MANIFEST = PROJECT_ROOT / "data/manifests/knowledge-v2.json"


class LocalReviewedIndexBackend:
    """合成草稿专用；生产 Milvus/持久 BM25 后端不由此类替代。"""

    def __init__(self, settings: RuntimeSettings, backend: str) -> None:
        if settings.app_env == "production":
            raise RuntimeError("生产环境不得使用本地合成知识索引")
        if backend not in ("vector", "bm25", "graph"):
            raise ValueError("未知索引后端")
        self._settings = settings
        self._backend = backend
        self._vector = None
        self._bm25 = None

    def _chunks(self, lease: KnowledgeIndexLease, manifest: bytes):
        if MANIFEST.read_bytes() != manifest:
            raise ValueError("持久化清单与受审阅本地清单不一致")
        chunks = load_manifest_chunks(CORPUS, MANIFEST)
        if not chunks or chunks[0].release_id != lease.knowledge_release_id:
            raise ValueError("本地切片发布版本不一致")
        if not any(chunk.tenant_id == lease.tenant_id for chunk in chunks):
            raise ValueError("本地切片不含目标租户")
        return chunks

    def build(self, lease: KnowledgeIndexLease, manifest: bytes) -> None:
        if lease.backend != self._backend:
            raise ValueError("任务与后端不一致")
        chunks = self._chunks(lease, manifest)
        if self._backend == "vector":
            built, vector, _ = build_local_retrievers(self._settings)
            if built != chunks:
                raise ValueError("向量构建切片与发布清单不一致")
            self._vector = vector
        elif self._backend == "bm25":
            adapter = BM25Adapter()
            adapter.build(chunks)
            self._bm25 = adapter
        else:
            driver = create_neo4j_driver(
                self._settings.neo4j_uri,
                self._settings.neo4j_username,
                self._settings.neo4j_password,
                self._settings.neo4j_connection_timeout_s,
            )
            try:
                seed_reviewed_graph(
                    driver,
                    self._settings.neo4j_database,
                    lease.tenant_id,
                    lease.knowledge_release_id,
                )
            finally:
                driver.close()

    def verify(self, lease: KnowledgeIndexLease, manifest: bytes) -> str:
        chunks = self._chunks(lease, manifest)
        if self._backend == "vector":
            if self._vector is None or self._vector._collection is None:
                raise ValueError("向量索引未构建")
            records = self._vector._collection.get(include=["metadatas", "documents"])
            actual = {
                chunk_id: (metadata, content)
                for chunk_id, metadata, content in zip(
                    records["ids"], records["metadatas"], records["documents"]
                )
            }
            expected = {chunk.chunk_id: chunk for chunk in chunks}
            if set(actual) != set(expected):
                raise ValueError("向量索引切片集合不完整")
            for chunk_id, chunk in expected.items():
                metadata, content = actual[chunk_id]
                if content != chunk.content or any(
                    metadata.get(field) != value
                    for field, value in (
                        ("tenant_id", chunk.tenant_id),
                        ("release_id", chunk.release_id),
                        ("document_id", chunk.document_id),
                        ("evidence_id", chunk.evidence_id),
                        ("version", chunk.version),
                        ("status", chunk.status),
                    )
                ):
                    raise ValueError("向量索引证据或版本读回不一致")
        elif self._backend == "bm25":
            if self._bm25 is None or self._bm25._chunks != chunks:
                raise ValueError("BM25 索引切片读回不一致")
        else:
            nodes, docs, edges = build_seed_plan(
                CORPUS, MANIFEST, lease.tenant_id, lease.knowledge_release_id
            )
            expected_nodes = {
                (node["props"]["node_id"], node["props"].get("evidence_id")) for node in nodes
            } | {(doc["node_id"], doc["evidence_id"]) for doc in docs}
            expected_edges = {
                (edge["props"]["edge_id"], edge["props"]["evidence_id"], edge["props"]["version"])
                for edge in edges
            }
            driver = create_neo4j_driver(
                self._settings.neo4j_uri,
                self._settings.neo4j_query_username,
                self._settings.neo4j_query_password,
                self._settings.neo4j_connection_timeout_s,
            )
            try:
                with driver.session(database=self._settings.neo4j_database) as session:
                    found_nodes = {
                        (row["node_id"], row["evidence_id"])
                        for row in session.run(
                            "MATCH (n {tenant_id: $tenant_id, knowledge_release_id: $release_id}) "
                            "RETURN n.node_id AS node_id, n.evidence_id AS evidence_id",
                            tenant_id=lease.tenant_id,
                            release_id=lease.knowledge_release_id,
                        )
                    }
                    found_edges = {
                        (row["edge_id"], row["evidence_id"], row["version"])
                        for row in session.run(
                            "MATCH ()-[r]->() WHERE r.tenant_id=$tenant_id "
                            "AND r.knowledge_release_id=$release_id "
                            "RETURN r.edge_id AS edge_id, r.evidence_id AS evidence_id, "
                            "r.version AS version",
                            tenant_id=lease.tenant_id,
                            release_id=lease.knowledge_release_id,
                        )
                    }
            finally:
                driver.close()
            if found_nodes != expected_nodes or found_edges != expected_edges:
                raise ValueError("Neo4j 节点、关系或证据读回不一致")
        return hashlib.sha256(manifest).hexdigest()
