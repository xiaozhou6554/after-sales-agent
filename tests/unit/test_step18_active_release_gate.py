"""知识查询仅使用已激活且三路索引同摘要成功的发布版本。"""

import hashlib

from sqlalchemy import create_engine, text

from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLActiveKnowledgeReleaseRepository,
)

MANIFEST = '{"release":"r1"}'
DIGEST = hashlib.sha256(MANIFEST.encode()).hexdigest()


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_release (tenant_id TEXT, knowledge_release_id TEXT, "
                "status TEXT, is_active INTEGER, manifest_digest TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_index_job (tenant_id TEXT, knowledge_release_id TEXT, "
                "backend TEXT, manifest_digest TEXT, status TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_manifest (tenant_id TEXT, knowledge_release_id TEXT, "
                "manifest_json TEXT, manifest_digest TEXT)"
            )
        )
    return engine


def test_active_release_requires_three_matching_successful_backends() -> None:
    engine = _engine()
    try:
        repo = MySQLActiveKnowledgeReleaseRepository(engine)
        with engine.begin() as connection:
            for tenant in ("north", "south"):
                connection.execute(
                    text("INSERT INTO knowledge_release VALUES (:tenant,'r1','ready',1,:digest)"),
                    {"tenant": tenant, "digest": DIGEST},
                )
                connection.execute(
                    text("INSERT INTO knowledge_manifest VALUES (:tenant,'r1',:manifest,:digest)"),
                    {"tenant": tenant, "manifest": MANIFEST, "digest": DIGEST},
                )
                for backend in ("vector", "bm25", "graph"):
                    connection.execute(
                        text(
                            "INSERT INTO knowledge_index_job VALUES "
                            "(:tenant,'r1',:backend,:digest,'succeeded')"
                        ),
                        {
                            "tenant": tenant,
                            "backend": backend,
                            "digest": (
                                "wrong-digest"
                                if tenant == "north" and backend == "graph"
                                else DIGEST
                            ),
                        },
                    )
            connection.execute(
                text("INSERT INTO knowledge_release VALUES ('north','draft','draft',0,'digest-2')")
            )
        assert repo.get_active_release_id("north") is None
        assert repo.get_active_release_id("south") == "r1"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE knowledge_index_job SET manifest_digest=:digest "
                    "WHERE tenant_id='north' AND backend='graph'"
                ),
                {"digest": DIGEST},
            )
        assert repo.get_active_release_id("north") == "r1"
    finally:
        engine.dispose()


def test_inactive_release_is_never_visible_even_with_complete_jobs() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO knowledge_release VALUES ('north','draft','ready',0,:digest)"),
                {"digest": DIGEST},
            )
            connection.execute(
                text("INSERT INTO knowledge_manifest VALUES ('north','draft',:manifest,:digest)"),
                {"manifest": MANIFEST, "digest": DIGEST},
            )
            for backend in ("vector", "bm25", "graph"):
                connection.execute(
                    text(
                        "INSERT INTO knowledge_index_job VALUES "
                        "('north','draft',:backend,:digest,'succeeded')"
                    ),
                    {"backend": backend, "digest": DIGEST},
                )
        assert MySQLActiveKnowledgeReleaseRepository(engine).get_active_release_id("north") is None
    finally:
        engine.dispose()


def test_modified_manifest_is_not_visible_even_when_jobs_succeeded() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO knowledge_release VALUES ('north','r1','ready',1,:digest)"),
                {"digest": DIGEST},
            )
            connection.execute(
                text("INSERT INTO knowledge_manifest VALUES ('north','r1','tampered',:digest)"),
                {"digest": DIGEST},
            )
            for backend in ("vector", "bm25", "graph"):
                connection.execute(
                    text(
                        "INSERT INTO knowledge_index_job VALUES "
                        "('north','r1',:backend,:digest,'succeeded')"
                    ),
                    {"backend": backend, "digest": DIGEST},
                )
        assert MySQLActiveKnowledgeReleaseRepository(engine).get_active_release_id("north") is None
    finally:
        engine.dispose()
