"""不可变清单与三路任务的原子候选登记；重复事件只读取既有记录。"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.knowledge.indexing.release_gate import BACKENDS
from application.knowledge.retrieval.ports import KnowledgeChunk


class MySQLKnowledgeManifestRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_manifest(self, tenant_id: str, knowledge_release_id: str) -> bytes:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT manifest_json,manifest_digest FROM knowledge_manifest "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id"
                ),
                {"tenant_id": tenant_id, "release_id": knowledge_release_id},
            ).first()
        if row is None:
            raise LookupError("知识发布清单不存在")
        raw = str(row[0]).encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != row[1]:
            raise ValueError("持久化知识清单摘要不匹配")
        return raw

    def register_candidate(
        self,
        tenant_id: str,
        version: str,
        manifest: bytes,
        chunks: tuple[KnowledgeChunk, ...],
        *,
        source_ref: str,
        connection: Connection | None = None,
    ) -> str:
        if not tenant_id or not version or not source_ref:
            raise ValueError("知识发布登记参数不能为空")
        try:
            raw = manifest.decode("utf-8")
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("知识发布清单不是有效 UTF-8 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("知识发布清单必须是对象")
        release_id = parsed.get("knowledge_release_id")
        locked = parsed.get("documents")
        if not isinstance(release_id, str) or not release_id or not isinstance(locked, list):
            raise ValueError("知识发布清单缺少版本或文档")
        if any(not isinstance(item, dict) for item in locked):
            raise ValueError("知识发布清单文档格式无效")
        relevant = [item for item in locked if item.get("tenant_id") == tenant_id]
        if not relevant or len(relevant) != len(chunks):
            raise ValueError("知识清单与当前租户切片数量不一致")
        by_id = {chunk.document_id: chunk for chunk in chunks}
        if len(by_id) != len(chunks):
            raise ValueError("同一发布的文档标识重复")
        for item in relevant:
            chunk = by_id.get(item.get("document_id"))
            if (
                chunk is None
                or chunk.tenant_id != tenant_id
                or chunk.release_id != release_id
                or str(item.get("version")) != str(chunk.version)
                or item.get("evidence_id") != chunk.evidence_id
                or item.get("chunk_id") != chunk.chunk_id
                or item.get("status") != chunk.status
                or item.get("content_sha256")
                != hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
            ):
                raise ValueError("知识切片与锁定清单不一致")
        digest = hashlib.sha256(manifest).hexdigest()
        transaction = self._engine.begin() if connection is None else nullcontext(connection)
        with transaction as connection:
            existing = connection.execute(
                text(
                    "SELECT version,manifest_digest FROM knowledge_release "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id"
                ),
                {"tenant_id": tenant_id, "release_id": release_id},
            ).first()
            if existing is not None:
                snapshot = connection.execute(
                    text(
                        "SELECT manifest_digest,manifest_json FROM knowledge_manifest "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id"
                    ),
                    {"tenant_id": tenant_id, "release_id": release_id},
                ).first()
                jobs = connection.execute(
                    text(
                        "SELECT backend,manifest_digest FROM knowledge_index_job "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id"
                    ),
                    {"tenant_id": tenant_id, "release_id": release_id},
                ).all()
                documents = connection.execute(
                    text(
                        "SELECT document_id,content_digest FROM knowledge_document "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id"
                    ),
                    {"tenant_id": tenant_id, "release_id": release_id},
                ).all()
                expected_documents = {
                    chunk.document_id: hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                    for chunk in chunks
                }
                if (
                    existing[0] != version
                    or existing[1] != digest
                    or snapshot is None
                    or snapshot[0] != digest
                    or snapshot[1] != raw
                    or {job[0] for job in jobs} != BACKENDS
                    or len(jobs) != len(BACKENDS)
                    or any(job[1] != digest for job in jobs)
                    or dict(documents) != expected_documents
                    or len(documents) != len(expected_documents)
                ):
                    raise ValueError("重复知识发布事件与不可变清单冲突")
                return "already_registered"
            connection.execute(
                text(
                    "INSERT INTO knowledge_release "
                    "(tenant_id,knowledge_release_id,version,manifest_digest,"
                    "status,revision,is_active) "
                    "VALUES (:tenant_id,:release_id,:version,:digest,'draft',0,0)"
                ),
                {
                    "tenant_id": tenant_id,
                    "release_id": release_id,
                    "version": version,
                    "digest": digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO knowledge_manifest "
                    "(tenant_id,knowledge_release_id,manifest_json,manifest_digest) "
                    "VALUES (:tenant_id,:release_id,:manifest,:digest)"
                ),
                {
                    "tenant_id": tenant_id,
                    "release_id": release_id,
                    "manifest": raw,
                    "digest": digest,
                },
            )
            for chunk in chunks:
                connection.execute(
                    text(
                        "INSERT INTO knowledge_document "
                        "(tenant_id,knowledge_release_id,document_id,title,source_ref,"
                        "content_digest,version,change_type) VALUES "
                        "(:tenant_id,:release_id,:document_id,:title,:source_ref,"
                        ":content_digest,:version,:change_type)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "release_id": release_id,
                        "document_id": chunk.document_id,
                        "title": chunk.title,
                        "source_ref": source_ref,
                        "content_digest": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                        "version": str(chunk.version),
                        "change_type": "delete" if chunk.status == "retired" else "upsert",
                    },
                )
            for backend in sorted(BACKENDS):
                job_id = hashlib.sha256(
                    f"{tenant_id}:{release_id}:{backend}".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    text(
                        "INSERT INTO knowledge_index_job "
                        "(tenant_id,job_id,knowledge_release_id,backend,manifest_digest,status) "
                        "VALUES (:tenant_id,:job_id,:release_id,:backend,:digest,'pending')"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "job_id": job_id,
                        "release_id": release_id,
                        "backend": backend,
                        "digest": digest,
                    },
                )
        return "registered"
