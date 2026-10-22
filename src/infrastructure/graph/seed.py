"""仅导入仓库内审阅过的 v2 知识图；供独立索引维护账号调用。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

from domain.knowledge.graph import NODE_IDS, RELATION_ENDPOINTS
from infrastructure.graph.schema import SCHEMA_STATEMENTS

_ROOT = Path(__file__).resolve().parents[3]
_CORPUS = _ROOT / "data/knowledge/v2/corpus.json"
_MANIFEST = _ROOT / "data/manifests/knowledge-v2.json"
_OPEN_END = "9999-12-31"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"图谱导入字段 {field} 缺失")
    return value


def _day(value: Any, field: str) -> str:
    value = _text(value, field)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"图谱导入日期 {field} 无效") from exc
    return value


def build_seed_plan(
    corpus_path: Path, manifest_path: Path, tenant_id: str, release_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """纯校验阶段；任何失败都发生在数据库写入之前。"""
    _text(tenant_id, "tenant_id")
    _text(release_id, "release_id")
    raw = corpus_path.read_bytes()
    corpus = json.loads(raw)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("files", {}).get("data/knowledge/v2/corpus.json")
        != hashlib.sha256(raw).hexdigest()
        or corpus.get("release_id") != release_id
        or manifest.get("knowledge_release_id") != release_id
    ):
        raise ValueError("图谱语料与发布清单或摘要不一致")
    locked = {(d["tenant_id"], d["document_id"], d["version"]): d for d in manifest["documents"]}
    docs: dict[str, dict[str, Any]] = {}
    for item in corpus["documents"]:
        if item["tenant_id"] != tenant_id or item["status"] != "published":
            continue
        key = (tenant_id, item["document_id"], item["version"])
        lock = locked.get(key)
        if (
            lock is None
            or lock["status"] != "published"
            or lock["evidence_id"] != item.get("evidence_id")
            or lock["content_sha256"] != hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            or lock["chunk_id"] != f"{item['document_id']}:v{item['version']}:0001"
        ):
            raise ValueError("图谱证据与发布清单不一致")
        if item["document_id"] in docs:
            raise ValueError("图谱证据文档标识重复")
        start = _day(item["valid_from"], "document.valid_from")
        end = _day(item["valid_to"] or _OPEN_END, "document.valid_to")
        if start > end:
            raise ValueError("图谱证据文档有效期倒置")
        docs[item["document_id"]] = {
            "tenant_id": tenant_id,
            "knowledge_release_id": release_id,
            "node_id": item["document_id"],
            "document_id": item["document_id"],
            "name": _text(item["title"], "title"),
            "title": item["title"],
            "source": "data/knowledge/v2/corpus.json",
            "version": str(item["version"]),
            "status": "published",
            "evidence_id": _text(item["evidence_id"], "evidence_id"),
            "chunk_id": lock["chunk_id"],
            "valid_from": start,
            "valid_to": end,
        }
    nodes: dict[str, dict[str, Any]] = {}
    for item in corpus["graph"]["nodes"]:
        if item["tenant_id"] != tenant_id:
            continue
        kind = item.get("kind")
        if kind not in NODE_IDS:
            raise ValueError("图谱节点类型不在白名单")
        node_id = _text(item.get("node_id"), "node_id")
        if node_id in nodes or not item.get(NODE_IDS[kind]) or not item.get("version"):
            raise ValueError("图谱节点业务标识或版本无效")
        props = {k: v for k, v in item.items() if k not in ("kind", "label")}
        props.update(tenant_id=tenant_id, knowledge_release_id=release_id)
        if kind == "Policy":
            start = _day(item.get("effective_from"), "policy.effective_from")
            end = _day(item.get("effective_to"), "policy.effective_to")
            if start > end or item.get("status") != "published":
                raise ValueError("图谱政策版本或有效期无效")
        nodes[node_id] = {"kind": kind, "props": props}
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    for item in corpus["graph"]["edges"]:
        if item["tenant_id"] != tenant_id:
            continue
        relation = item.get("relation")
        start_node, end_node = nodes.get(item.get("from")), nodes.get(item.get("to"))
        if relation not in RELATION_ENDPOINTS or not start_node or not end_node:
            raise ValueError("图谱关系类型或端点不存在")
        if (start_node["kind"], end_node["kind"]) != RELATION_ENDPOINTS[relation]:
            raise ValueError("图谱关系端点类型不匹配")
        edge_id = _text(item.get("edge_id"), "edge_id")
        if edge_id in seen_edges:
            raise ValueError("图谱关系标识重复")
        seen_edges.add(edge_id)
        document = docs.get(item.get("document_id"))
        if not document or document["evidence_id"] != item.get("evidence_id"):
            raise ValueError("图谱关系没有已发布的同租户证据")
        if item.get("version") != f"{document['document_id']}:v{document['version']}":
            raise ValueError("图谱关系版本与证据不一致")
        policy = end_node if end_node["kind"] == "Policy" else start_node
        if policy["kind"] == "Policy" and str(policy["props"]["version"]) != document["version"]:
            raise ValueError("图谱政策版本与证据不一致")
        start = _day(item.get("valid_from"), "relation.valid_from")
        end = _day(item.get("valid_to"), "relation.valid_to")
        if start > end or start < document["valid_from"] or end > document["valid_to"]:
            raise ValueError("图谱关系有效期超出证据有效期")
        props = {k: v for k, v in item.items() if k not in ("from", "to", "relation")}
        props.update(tenant_id=tenant_id, knowledge_release_id=release_id)
        edges.append(
            {
                "relation": relation,
                "from": item["from"],
                "to": item["to"],
                "from_kind": start_node["kind"],
                "to_kind": end_node["kind"],
                "props": props,
            }
        )
    return list(nodes.values()), list(docs.values()), edges


def seed_reviewed_graph(
    driver: Driver, database: str, tenant_id: str, release_id: str
) -> tuple[int, int]:
    """幂等导入指定租户/发布的已审阅语料；调用方须使用索引维护凭据。"""
    if not database.strip():
        raise ValueError("Neo4j database 不得为空")
    nodes, docs, edges = build_seed_plan(_CORPUS, _MANIFEST, tenant_id, release_id)

    # 只由维护凭据调用；先完成幂等 schema 安装，再写入固定语料。
    with driver.session(database=database) as session:
        for statement in SCHEMA_STATEMENTS:
            session.execute_write(lambda tx, query=statement: tx.run(query).consume())

    def write(tx: Any) -> None:
        for node in nodes:
            kind = node["kind"]  # 已经由固定 NODE_IDS 白名单验证
            tx.run(
                f"MERGE (n:{kind} {{tenant_id: $tenant_id, "
                "knowledge_release_id: $release_id, node_id: $node_id}) SET n += $props",
                tenant_id=tenant_id,
                release_id=release_id,
                node_id=node["props"]["node_id"],
                props=node["props"],
            ).consume()
        for doc in docs:
            tx.run(
                "MERGE (n:KnowledgeDocument {tenant_id: $tenant_id, "
                "knowledge_release_id: $release_id, node_id: $node_id}) SET n += $props",
                tenant_id=tenant_id,
                release_id=release_id,
                node_id=doc["node_id"],
                props=doc,
            ).consume()
        for edge in edges:
            relation = edge["relation"]  # 已经由固定 RELATION_ENDPOINTS 白名单验证
            from_kind, to_kind = RELATION_ENDPOINTS[relation]
            tx.run(
                f"MATCH (a:{from_kind} {{tenant_id: $tenant_id, knowledge_release_id: $release_id, "
                "node_id: $from_id}) "
                f"MATCH (b:{to_kind} {{tenant_id: $tenant_id, knowledge_release_id: $release_id, "
                "node_id: $to_id}) "
                f"MERGE (a)-[r:{relation} {{tenant_id: $tenant_id, "
                "knowledge_release_id: $release_id, edge_id: $edge_id}]->(b) "
                "SET r += $props",
                tenant_id=tenant_id,
                release_id=release_id,
                from_id=edge["from"],
                to_id=edge["to"],
                edge_id=edge["props"]["edge_id"],
                props=edge["props"],
            ).consume()

    with driver.session(database=database) as session:
        session.execute_write(write)
    return len(nodes) + len(docs), len(edges)
