"""步骤21本机合成版本三路构建、读回与条件激活；绝不在生产运行。"""

from __future__ import annotations

import json
import time

from application.knowledge.indexing.worker import KnowledgeIndexWorker
from bootstrap.local_knowledge_indexing import CORPUS, MANIFEST, LocalReviewedIndexBackend
from bootstrap.settings import load_settings
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.knowledge_index_job_repository import (
    MySQLKnowledgeIndexJobRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_manifest_repository import (
    MySQLKnowledgeManifestRepository,
)
from infrastructure.persistence.mysql.repositories.knowledge_release_repository import (
    MySQLActiveKnowledgeReleaseRepository,
    MySQLKnowledgeReleasePublisher,
)


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production" or not settings.knowledge_local_tools_enabled:
        raise RuntimeError("仅允许在启用本地知识工具的开发环境发布合成知识")
    if not settings.neo4j_query_password:
        raise RuntimeError("缺少独立 Neo4j 只读查询凭据")
    manifest = MANIFEST.read_bytes()
    payload = json.loads(manifest)
    release_id = payload["knowledge_release_id"]
    all_chunks = load_manifest_chunks(CORPUS, MANIFEST)
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    started = time.monotonic()
    try:
        snapshots = MySQLKnowledgeManifestRepository(engine)
        jobs = MySQLKnowledgeIndexJobRepository(engine)
        publisher = MySQLKnowledgeReleasePublisher(engine)
        active = MySQLActiveKnowledgeReleaseRepository(engine)
        results = []
        for tenant_id in ("tenant-north", "tenant-south"):
            chunks = tuple(chunk for chunk in all_chunks if chunk.tenant_id == tenant_id)
            registration = snapshots.register_candidate(
                tenant_id,
                release_id,
                manifest,
                chunks,
                source_ref="data/knowledge/v2/corpus.json",
            )
            backends = {
                name: LocalReviewedIndexBackend(settings, name)
                for name in ("vector", "bm25", "graph")
            }
            worker = KnowledgeIndexWorker(jobs, snapshots, backends)
            outcomes = {
                name: worker.run_once(tenant_id, release_id, name, "local-step21")
                for name in ("vector", "bm25", "graph")
            }
            if any(result == "failed" for result in outcomes.values()):
                raise RuntimeError("至少一路索引构建或读回失败，候选版本未激活")
            revision = publisher.get_revision(tenant_id, release_id)
            if revision is None:
                raise RuntimeError("候选版本登记后不可见")
            activation = publisher.activate_if_complete(tenant_id, release_id, revision)
            if activation not in ("activated", "already_active"):
                raise RuntimeError("三路索引未全部满足同一清单发布门禁")
            if active.get_active_release_id(tenant_id) != release_id:
                raise RuntimeError("激活后的知识版本不可查询")
            results.append(
                {
                    "tenant_id": tenant_id,
                    "registration": registration,
                    "backends": outcomes,
                    "activation": activation,
                }
            )
        print(
            json.dumps(
                {
                    "release_id": release_id,
                    "synthetic_only": True,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "results": results,
                },
                ensure_ascii=False,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
