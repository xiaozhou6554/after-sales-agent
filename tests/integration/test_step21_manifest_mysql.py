"""本机 MySQL：清单与三任务原子登记、重复事件和租约领取。"""

from __future__ import annotations

import uuid
from pathlib import Path

from bootstrap.settings import load_settings
from sqlalchemy import text

from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.knowledge_index_job_repository import (
    MySQLKnowledgeIndexJobRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_manifest_repository import (
    MySQLKnowledgeManifestRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLKnowledgeReleasePublisher,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data/knowledge/v2/corpus.json"
MANIFEST = ROOT / "data/manifests/knowledge-v2.json"


def test_real_mysql_manifest_register_and_claim() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    # 已审阅清单只包含固定租户；为避免污染真实租户，只在测试事务内复制为唯一租户。
    import json

    source = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tenant = f"step21-{uuid.uuid4().hex[:12]}"
    original = "tenant-north"
    for document in source["documents"]:
        if document["tenant_id"] == original:
            document["tenant_id"] = tenant
    raw = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    original_chunks = load_manifest_chunks(CORPUS, MANIFEST)
    chunks = tuple(
        chunk.__class__(
            tenant,
            chunk.release_id,
            chunk.chunk_id,
            chunk.document_id,
            chunk.evidence_id,
            chunk.version,
            chunk.title,
            chunk.content,
            chunk.search_text,
            chunk.status,
        )
        for chunk in original_chunks
        if chunk.tenant_id == original
    )
    release_id = source["knowledge_release_id"]
    try:
        repo = MySQLKnowledgeManifestRepository(engine)
        assert (
            repo.register_candidate(
                tenant, "mock-v2", raw, chunks, source_ref="data/knowledge/v2/corpus.json"
            )
            == "registered"
        )
        assert repo.get_manifest(tenant, release_id) == raw
        assert (
            repo.register_candidate(
                tenant, "mock-v2", raw, chunks, source_ref="data/knowledge/v2/corpus.json"
            )
            == "already_registered"
        )
        jobs = MySQLKnowledgeIndexJobRepository(engine)
        lease = jobs.claim(tenant, release_id, "vector", "worker-test")
        assert lease is not None and lease.revision == 1
        assert jobs.claim(tenant, release_id, "vector", "worker-other") is None
        assert (
            MySQLKnowledgeReleasePublisher(engine).activate_if_complete(tenant, release_id, 1)
            == "pending"
        )
    finally:
        with engine.begin() as connection:
            params = {"tenant": tenant}
            connection.execute(
                text("DELETE FROM knowledge_index_job WHERE tenant_id=:tenant"), params
            )
            connection.execute(
                text("DELETE FROM knowledge_document WHERE tenant_id=:tenant"), params
            )
            connection.execute(
                text("DELETE FROM knowledge_manifest WHERE tenant_id=:tenant"), params
            )
            connection.execute(
                text("DELETE FROM knowledge_release WHERE tenant_id=:tenant"), params
            )
        engine.dispose()
