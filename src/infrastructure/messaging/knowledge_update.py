"""knowledge.updated Inbox 处理：在消费事务内登记清单与三路任务。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import Connection

from application.common.events import EventEnvelope, KnowledgeUpdatedPayload
from application.knowledge.retrieval.ports import KnowledgeChunk
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.persistence.mysql.repositories.knowledge_manifest_repository import (
    MySQLKnowledgeManifestRepository,
)


class KnowledgeManifestSource(Protocol):
    def load(
        self, tenant_id: str, knowledge_release_id: str
    ) -> tuple[bytes, tuple[KnowledgeChunk, ...]]: ...


class LocalReviewedManifestSource:
    """只读本地受审阅合成语料；生产环境须替换为受控对象存储来源。"""

    def __init__(self, corpus_path: Path, manifest_path: Path) -> None:
        self._corpus_path = corpus_path
        self._manifest_path = manifest_path

    def load(
        self, tenant_id: str, knowledge_release_id: str
    ) -> tuple[bytes, tuple[KnowledgeChunk, ...]]:
        raw = self._manifest_path.read_bytes()
        manifest = json.loads(raw)
        if manifest.get("knowledge_release_id") != knowledge_release_id:
            raise ValueError("事件发布版本与来源清单不一致")
        chunks = tuple(
            chunk
            for chunk in load_manifest_chunks(self._corpus_path, self._manifest_path)
            if chunk.tenant_id == tenant_id
        )
        if not chunks:
            raise ValueError("事件租户在来源清单中无文档")
        return raw, chunks


def knowledge_updated_handler(
    repository: MySQLKnowledgeManifestRepository,
    source: KnowledgeManifestSource,
    *,
    source_ref: str,
):
    def handle(connection: Connection, event: EventEnvelope) -> str:
        if event.event_type != "knowledge.updated" or not isinstance(
            event.payload, KnowledgeUpdatedPayload
        ):
            raise ValueError("非知识更新事件")
        payload = event.payload
        raw, chunks = source.load(event.tenant_id, payload.knowledge_release_id)
        if hashlib.sha256(raw).hexdigest() != payload.manifest_digest:
            raise ValueError("事件清单摘要与来源不一致")
        match = next((chunk for chunk in chunks if chunk.document_id == payload.document_id), None)
        if match is None or str(match.version) != payload.version:
            raise ValueError("事件文档或版本不在目标清单")
        if payload.change_type not in ("upsert", "delete"):
            raise ValueError("知识变更类型无效")
        if payload.change_type == "delete" and match.status != "retired":
            raise ValueError("删除事件与清单文档状态不一致")
        result = repository.register_candidate(
            event.tenant_id,
            payload.knowledge_release_id,
            raw,
            chunks,
            source_ref=source_ref,
            connection=connection,
        )
        if result not in ("registered", "already_registered"):
            raise RuntimeError("知识候选登记未完成")
        return payload.knowledge_release_id

    return handle
