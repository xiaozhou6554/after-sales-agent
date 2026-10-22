"""本地合成知识清单的政策元数据适配器；生产需独立发布源。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from application.knowledge.policy import PolicyDocument
from infrastructure.document.knowledge_loader import load_manifest_chunks


class LocalManifestPolicySource:
    def __init__(self, corpus_path: Path, manifest_path: Path) -> None:
        # 先执行步骤17的全部文档/证据/内容哈希校验，再投影政策元数据。
        chunks = load_manifest_chunks(corpus_path, manifest_path)
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        if not corpus.get("synthetic_only"):
            raise ValueError("本地政策适配器仅用于合成知识")
        by_key = {(chunk.tenant_id, chunk.document_id, chunk.version): chunk for chunk in chunks}
        policies = []
        for item in corpus["documents"]:
            if not item.get("policy_id"):
                continue
            key = (item["tenant_id"], item["document_id"], item["version"])
            chunk = by_key[key]
            policies.append(
                PolicyDocument(
                    tenant_id=item["tenant_id"],
                    knowledge_release_id=corpus["release_id"],
                    policy_id=item["policy_id"],
                    document_id=item["document_id"],
                    policy_name=item["title"],
                    aliases=tuple(item.get("aliases") or ()),
                    clauses=(chunk.content,),
                    # 合成语料没有独立结构化适用条件；不得从自由文本猜造。
                    applicability=tuple(item.get("applicability") or ()),
                    valid_from=date.fromisoformat(item["valid_from"]),
                    valid_to=date.fromisoformat(item["valid_to"]) if item["valid_to"] else None,
                    version=str(item["version"]),
                    evidence_id=chunk.evidence_id,
                    status=item["status"],
                )
            )
        self._policies = tuple(policies)

    def list_for_release(
        self, tenant_id: str, knowledge_release_id: str
    ) -> tuple[PolicyDocument, ...]:
        return tuple(
            item
            for item in self._policies
            if item.tenant_id == tenant_id and item.knowledge_release_id == knowledge_release_id
        )
