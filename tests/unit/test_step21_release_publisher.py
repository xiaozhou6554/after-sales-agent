"""步骤21事务化激活：缺路或失败保留旧发布。"""

import hashlib

from sqlalchemy import create_engine, text

from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLActiveKnowledgeReleaseRepository,
    MySQLKnowledgeReleasePublisher,
)

OLD_MANIFEST = '{"release":"old"}'
NEW_MANIFEST = '{"release":"new"}'
OLD_DIGEST = hashlib.sha256(OLD_MANIFEST.encode()).hexdigest()
NEW_DIGEST = hashlib.sha256(NEW_MANIFEST.encode()).hexdigest()


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_release (tenant_id TEXT, knowledge_release_id TEXT, "
                "manifest_digest TEXT, status TEXT, revision INTEGER, is_active INTEGER, "
                "published_at TEXT, failure_reason TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_index_job (tenant_id TEXT, knowledge_release_id TEXT, "
                "backend TEXT, manifest_digest TEXT, status TEXT, revision INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_manifest (tenant_id TEXT,knowledge_release_id TEXT,"
                "manifest_json TEXT,manifest_digest TEXT)"
            )
        )
        for release, manifest, digest, status, revision, active in (
            ("old", OLD_MANIFEST, OLD_DIGEST, "ready", 1, 1),
            ("new", NEW_MANIFEST, NEW_DIGEST, "draft", 0, 0),
        ):
            connection.execute(
                text(
                    "INSERT INTO knowledge_release VALUES "
                    "('north',:release,:digest,:status,:revision,:active,NULL,NULL)"
                ),
                {
                    "release": release,
                    "digest": digest,
                    "status": status,
                    "revision": revision,
                    "active": active,
                },
            )
            connection.execute(
                text("INSERT INTO knowledge_manifest VALUES ('north',:release,:manifest,:digest)"),
                {"release": release, "manifest": manifest, "digest": digest},
            )
        for backend in ("vector", "bm25", "graph"):
            connection.execute(
                text(
                    "INSERT INTO knowledge_index_job VALUES "
                    "('north','old',:backend,:digest,'succeeded',1)"
                ),
                {"backend": backend, "digest": OLD_DIGEST},
            )
    return engine


def test_publisher_waits_for_all_three_then_switches_once() -> None:
    engine = _engine()
    try:
        publisher = MySQLKnowledgeReleasePublisher(engine)
        active = MySQLActiveKnowledgeReleaseRepository(engine)
        assert publisher.activate_if_complete("north", "new", 0) == "pending"
        assert active.get_active_release_id("north") == "old"
    finally:
        engine.dispose()


def test_successful_atomic_switch_and_revision_check() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            for backend in ("vector", "bm25", "graph"):
                connection.execute(
                    text(
                        "INSERT INTO knowledge_index_job VALUES "
                        "('north','new',:backend,:digest,'succeeded',1)"
                    ),
                    {"backend": backend, "digest": NEW_DIGEST},
                )
        publisher = MySQLKnowledgeReleasePublisher(engine)
        active = MySQLActiveKnowledgeReleaseRepository(engine)
        assert publisher.activate_if_complete("north", "new", 1) == "revision_conflict"
        assert active.get_active_release_id("north") == "old"
        assert publisher.activate_if_complete("north", "new", 0) == "activated"
        assert active.get_active_release_id("north") == "new"
        assert publisher.activate_if_complete("north", "new", 0) == "already_active"
    finally:
        engine.dispose()
