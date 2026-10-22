"""按步骤16锁定清单读取同批候选切片，验证内容哈希与版本。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from application.knowledge.retrieval.ports import KnowledgeChunk


def load_manifest_chunks(corpus_path: Path, manifest_path: Path) -> list[KnowledgeChunk]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release_id = manifest["knowledge_release_id"]
    if corpus["release_id"] != release_id:
        raise ValueError("知识文档与清单的发布版本不一致")
    by_key = {
        (item["tenant_id"], item["document_id"], item["version"]): item
        for item in corpus["documents"]
    }
    chunks: list[KnowledgeChunk] = []
    for locked in manifest["documents"]:
        key = (locked["tenant_id"], locked["document_id"], locked["version"])
        item = by_key.get(key)
        if item is None or item["evidence_id"] != locked["evidence_id"]:
            raise ValueError(f"知识清单文档证据不匹配: {key}")
        digest = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        if digest != locked["content_sha256"] or item["status"] != locked["status"]:
            raise ValueError(f"知识清单内容或状态不匹配: {key}")
        expected_chunk_id = f"{item['document_id']}:v{item['version']}:0001"
        if locked["chunk_id"] != expected_chunk_id:
            raise ValueError(f"知识切片标识不匹配: {key}")
        search_text = " ".join(
            [
                item["title"],
                *(item.get("aliases") or []),
                *(item.get("tags") or []),
                item["content"],
            ]
        )
        chunks.append(
            KnowledgeChunk(
                tenant_id=item["tenant_id"],
                release_id=release_id,
                chunk_id=expected_chunk_id,
                document_id=item["document_id"],
                evidence_id=item["evidence_id"],
                version=item["version"],
                title=item["title"],
                content=item["content"],
                search_text=search_text,
                status=item["status"],
            )
        )
    if len(by_key) != len(chunks):
        raise ValueError("候选知识文档未全部进入发布清单")
    return chunks
