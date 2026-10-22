"""候选清单、文档与三路任务同事务登记且重复事件不改写。"""

import hashlib
import json

import pytest
from sqlalchemy import create_engine, text

from application.knowledge.retrieval.ports import KnowledgeChunk
from infrastructure.persistence.mysql.repositories.knowledge_manifest_repository import (
    MySQLKnowledgeManifestRepository,
)


def _case():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_release (tenant_id TEXT,knowledge_release_id TEXT,"
                "version TEXT,manifest_digest TEXT,status TEXT,revision INTEGER,is_active INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_manifest (tenant_id TEXT,knowledge_release_id TEXT,"
                "manifest_json TEXT,manifest_digest TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_document (tenant_id TEXT,knowledge_release_id TEXT,"
                "document_id TEXT,title TEXT,source_ref TEXT,content_digest TEXT,"
                "version TEXT,change_type TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_index_job (tenant_id TEXT,job_id TEXT,"
                "knowledge_release_id TEXT,backend TEXT,manifest_digest TEXT,status TEXT)"
            )
        )
    chunk = KnowledgeChunk(
        "t1",
        "r1",
        "doc1:v1:0001",
        "doc1",
        "ev1",
        1,
        "Title",
        "Content",
        "Title Content",
        "published",
    )
    manifest = json.dumps(
        {
            "knowledge_release_id": "r1",
            "documents": [
                {
                    "tenant_id": "t1",
                    "document_id": "doc1",
                    "chunk_id": "doc1:v1:0001",
                    "version": 1,
                    "evidence_id": "ev1",
                    "status": "published",
                    "content_sha256": hashlib.sha256(b"Content").hexdigest(),
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    return engine, manifest, (chunk,)


def test_register_candidate_is_atomic_and_idempotent() -> None:
    engine, manifest, chunks = _case()
    try:
        repo = MySQLKnowledgeManifestRepository(engine)
        assert repo.register_candidate("t1", "v1", manifest, chunks, source_ref="synthetic") == (
            "registered"
        )
        assert repo.get_manifest("t1", "r1") == manifest
        assert repo.register_candidate("t1", "v1", manifest, chunks, source_ref="synthetic") == (
            "already_registered"
        )
        with engine.connect() as connection:
            jobs = (
                connection.execute(text("SELECT backend FROM knowledge_index_job")).scalars().all()
            )
            docs = connection.execute(text("SELECT document_id FROM knowledge_document")).all()
        assert set(jobs) == {"vector", "bm25", "graph"}
        assert docs == [("doc1",)]
    finally:
        engine.dispose()


def test_conflicting_duplicate_and_bad_chunk_leave_snapshot_unchanged() -> None:
    engine, manifest, chunks = _case()
    try:
        repo = MySQLKnowledgeManifestRepository(engine)
        with pytest.raises(ValueError):
            repo.register_candidate("t1", "v1", manifest, (), source_ref="synthetic")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM knowledge_release")).scalar() == 0
        repo.register_candidate("t1", "v1", manifest, chunks, source_ref="synthetic")
        with pytest.raises(ValueError):
            repo.register_candidate("t1", "v2", manifest, chunks, source_ref="synthetic")
        assert repo.get_manifest("t1", "r1") == manifest
    finally:
        engine.dispose()


def test_duplicate_event_rejects_missing_document_row() -> None:
    engine, manifest, chunks = _case()
    try:
        repo = MySQLKnowledgeManifestRepository(engine)
        repo.register_candidate("t1", "v1", manifest, chunks, source_ref="synthetic")
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM knowledge_document WHERE document_id='doc1'"))
        with pytest.raises(ValueError, match="不可变清单冲突"):
            repo.register_candidate("t1", "v1", manifest, chunks, source_ref="synthetic")
    finally:
        engine.dispose()
