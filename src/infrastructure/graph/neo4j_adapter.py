"""只接受结构化参数的 Neo4j 知识图查询。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

from application.knowledge.graph.ports import (
    GraphEvidence,
    GraphIntegrityError,
    GraphNode,
    GraphQuery,
    GraphRelation,
    GraphResult,
    GraphScope,
)
from domain.knowledge.graph import NODE_IDS, RELATION_ENDPOINTS, valid_interval

# 固定模板：所有用户数据均走参数，关系类型与返回顺序不能由调用方拼接。
GRAPH_QUERY = """
MATCH (a)-[r]->(b)
WHERE a.tenant_id = $tenant_id AND b.tenant_id = $tenant_id
  AND r.tenant_id = $tenant_id
  AND a.knowledge_release_id = $release_id
  AND b.knowledge_release_id = $release_id
  AND r.knowledge_release_id = $release_id
  AND (a.name IN $entity_names OR a.model IN $entity_names
       OR b.name IN $entity_names OR b.model IN $entity_names)
  AND type(r) IN $relation_types
  AND any(path IN $approved_paths WHERE path[0] IN labels(a)
          AND path[1] = type(r) AND path[2] IN labels(b))
  AND ($authorized_node_ids IS NULL OR
       (a.node_id IN $authorized_node_ids AND b.node_id IN $authorized_node_ids))
  AND r.version IS NOT NULL AND trim(toString(r.version)) <> ''
  AND r.evidence_id IS NOT NULL AND trim(r.evidence_id) <> ''
  AND r.valid_from IS NOT NULL AND r.valid_to IS NOT NULL
  AND date(r.valid_from) <= date($as_of) AND date($as_of) <= date(r.valid_to)
  AND (NOT a:Policy OR (a.status = 'published' AND a.effective_from IS NOT NULL
       AND a.effective_to IS NOT NULL AND date(a.effective_from) <= date($as_of)
       AND date($as_of) <= date(a.effective_to)))
  AND (NOT b:Policy OR (b.status = 'published' AND b.effective_from IS NOT NULL
       AND b.effective_to IS NOT NULL AND date(b.effective_from) <= date($as_of)
       AND date($as_of) <= date(b.effective_to)))
MATCH (e:KnowledgeDocument)
WHERE e.tenant_id = $tenant_id AND e.knowledge_release_id = $release_id
  AND e.document_id = r.document_id AND e.evidence_id = r.evidence_id
  AND e.status = 'published' AND e.version IS NOT NULL
  AND e.valid_from IS NOT NULL AND e.valid_to IS NOT NULL
  AND date(e.valid_from) <= date($as_of) AND date($as_of) <= date(e.valid_to)
  AND (r.chunk_id IS NULL OR e.chunk_id = r.chunk_id)
RETURN labels(a) AS a_labels, properties(a) AS a_props,
       type(r) AS relation_type, properties(r) AS r_props,
       labels(b) AS b_labels, properties(b) AS b_props,
       properties(e) AS e_props
ORDER BY a.name, type(r), b.name, r.evidence_id
LIMIT $limit
"""


def _required(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise GraphIntegrityError("图谱结果缺少必填标识或版本")
    return str(value)


def _date(value: Any) -> date:
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise GraphIntegrityError("图谱有效期无效") from exc


def _node(labels: list[str], props: dict[str, Any], scope: GraphScope, as_of: date) -> GraphNode:
    known = set(labels).intersection(NODE_IDS)
    if len(known) != 1:
        raise GraphIntegrityError("图谱节点类型不在白名单")
    kind = known.pop()
    node_id = _required(props.get("node_id"))
    if props.get(NODE_IDS[kind]) != node_id:
        # 政策在语料中使用独立 node_id；其业务 ID 仍必须存在。
        if kind != "Policy" or not props.get("policy_id"):
            raise GraphIntegrityError("图谱节点业务标识不一致")
    if (
        props.get("tenant_id") != scope.tenant_id
        or props.get("knowledge_release_id") != scope.knowledge_release_id
    ):
        raise GraphIntegrityError("图谱节点跨越查询作用域")
    if scope.authorized_node_ids is not None and node_id not in scope.authorized_node_ids:
        raise GraphIntegrityError("图谱节点不在授权范围")
    if kind == "Policy":
        if props.get("status") != "published" or not valid_interval(
            _date(props.get("effective_from")), _date(props.get("effective_to")), as_of
        ):
            raise GraphIntegrityError("政策无效或已过期")
    return GraphNode(node_id, kind, _required(props.get("name")), _required(props.get("version")))


class Neo4jGraphAdapter:
    def __init__(self, driver: Driver, database: str) -> None:
        if not database.strip():
            raise ValueError("Neo4j database 不得为空")
        self._driver = driver
        self._database = database

    def search(self, query: GraphQuery, scope: GraphScope) -> GraphResult:
        if not scope.tenant_id.strip() or not scope.knowledge_release_id.strip():
            raise ValueError("租户和知识发布版本必须由可信调用方提供")
        if (
            not isinstance(query.limit, int)
            or isinstance(query.limit, bool)
            or not 1 <= query.limit <= 50
        ):
            raise ValueError("图谱 limit 必须在 1 到 50 之间")
        if not 1 <= len(query.entity_names) <= 20 or any(
            not isinstance(name, str) or not 1 <= len(name.strip()) <= 200
            for name in query.entity_names
        ):
            raise ValueError("entity_names 必须包含 1 到 20 个非空名称")
        if not 1 <= len(query.relation_types) <= 20 or any(
            relation not in RELATION_ENDPOINTS for relation in query.relation_types
        ):
            raise ValueError("关系类型不在知识图谱白名单")
        if query.as_of is not None and (
            not isinstance(query.as_of, date) or isinstance(query.as_of, datetime)
        ):
            raise ValueError("as_of 必须是日期")
        as_of = query.as_of or datetime.now(timezone.utc).date()
        empty = GraphResult(scope.knowledge_release_id, (), (), ())
        if scope.authorized_node_ids is not None and not scope.authorized_node_ids:
            return empty
        params = {
            "tenant_id": scope.tenant_id,
            "release_id": scope.knowledge_release_id,
            "entity_names": list(query.entity_names),
            "relation_types": list(query.relation_types),
            "approved_paths": [
                [RELATION_ENDPOINTS[k][0], k, RELATION_ENDPOINTS[k][1]]
                for k in query.relation_types
            ],
            "authorized_node_ids": sorted(scope.authorized_node_ids)
            if scope.authorized_node_ids is not None
            else None,
            "as_of": as_of.isoformat(),
            "limit": query.limit,
        }
        from neo4j import READ_ACCESS

        with self._driver.session(
            database=self._database, default_access_mode=READ_ACCESS
        ) as session:
            rows = session.execute_read(lambda tx: list(tx.run(GRAPH_QUERY, **params)))

        nodes: dict[str, GraphNode] = {}
        relations: list[GraphRelation] = []
        evidence: dict[str, GraphEvidence] = {}
        for row in rows:
            a = _node(row["a_labels"], row["a_props"], scope, as_of)
            b = _node(row["b_labels"], row["b_props"], scope, as_of)
            relation_type = row["relation_type"]
            if relation_type not in query.relation_types or RELATION_ENDPOINTS.get(
                relation_type
            ) != (a.node_type, b.node_type):
                raise GraphIntegrityError("图谱关系类型或端点不合法")
            r, e = row["r_props"], row["e_props"]
            if any(
                props.get("tenant_id") != scope.tenant_id
                or props.get("knowledge_release_id") != scope.knowledge_release_id
                for props in (r, e)
            ):
                raise GraphIntegrityError("图谱关系或证据跨越查询作用域")
            if not valid_interval(_date(r.get("valid_from")), _date(r.get("valid_to")), as_of):
                raise GraphIntegrityError("图谱关系已过期")
            if e.get("status") != "published" or not valid_interval(
                _date(e.get("valid_from")), _date(e.get("valid_to")), as_of
            ):
                raise GraphIntegrityError("图谱证据无效或已过期")
            evidence_id = _required(r.get("evidence_id"))
            if e.get("evidence_id") != evidence_id or r.get("document_id") != e.get("document_id"):
                raise GraphIntegrityError("图谱关系证据引用不一致")
            if str(r.get("version")) != f"{e['document_id']}:v{e['version']}":
                raise GraphIntegrityError("图谱关系版本与证据不一致")
            if r.get("chunk_id") is not None and r["chunk_id"] != e.get("chunk_id"):
                raise GraphIntegrityError("图谱切片证据引用不一致")
            nodes[a.node_id], nodes[b.node_id] = a, b
            relations.append(
                GraphRelation(
                    a.node_id, relation_type, b.node_id, _required(r.get("version")), evidence_id
                )
            )
            evidence[evidence_id] = GraphEvidence(
                evidence_id,
                _required(e.get("document_id")),
                _required(e.get("source")),
                _required(e.get("version")),
            )
        return GraphResult(
            scope.knowledge_release_id,
            tuple(nodes.values()),
            tuple(relations),
            tuple(evidence.values()),
        )
