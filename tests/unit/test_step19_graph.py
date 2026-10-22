"""步骤19：固定图查询、作用域和证据守卫。"""

from __future__ import annotations

import sys
import types
from copy import deepcopy
from datetime import date

import pytest

from application.knowledge.graph.ports import GraphIntegrityError, GraphQuery, GraphScope
from infrastructure.graph.neo4j_adapter import GRAPH_QUERY, Neo4jGraphAdapter
from infrastructure.graph.seed import _CORPUS, _MANIFEST, build_seed_plan

RELEASE = "KNOWLEDGE-RELEASE-2026-10"


class Result(list):
    def consume(self):
        return None


class FakeTx:
    def __init__(self, driver):
        self.driver = driver

    def run(self, cypher, **params):
        self.driver.calls.append((cypher, params))
        return Result(self.driver.rows)


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute_read(self, action):
        return action(FakeTx(self.driver))

    def execute_write(self, action):
        return action(FakeTx(self.driver))


class FakeDriver:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []
        self.sessions = []

    def session(self, **kwargs):
        self.sessions.append(kwargs)
        return FakeSession(self)


@pytest.fixture(autouse=True)
def neo4j_read_constant(monkeypatch):
    # 本地单测可在未安装 Neo4j SDK 的轻量 Python 环境运行。
    try:
        import neo4j  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "neo4j", types.SimpleNamespace(READ_ACCESS="READ"))


def _row(relation_type="COMPATIBLE_WITH"):
    nodes, docs, edges = build_seed_plan(_CORPUS, _MANIFEST, "tenant-north", RELEASE)
    by_id = {n["props"]["node_id"]: n for n in nodes}
    edge = next(e for e in edges if e["relation"] == relation_type)
    document = next(d for d in docs if d["document_id"] == edge["props"]["document_id"])
    return {
        "a_labels": [by_id[edge["from"]]["kind"]],
        "a_props": by_id[edge["from"]]["props"],
        "relation_type": edge["relation"],
        "r_props": edge["props"],
        "b_labels": [by_id[edge["to"]]["kind"]],
        "b_props": by_id[edge["to"]]["props"],
        "e_props": document,
    }


def _search(driver, *, query=None, scope=None):
    return Neo4jGraphAdapter(driver, "after-sales-knowledge").search(
        query or GraphQuery(("Air purifier A",), ("COMPATIBLE_WITH",), as_of=date(2026, 10, 20)),
        scope or GraphScope("tenant-north", RELEASE),
    )


def test_parameterized_read_only_query_and_traceable_dto():
    driver = FakeDriver([_row()])
    result = _search(driver)
    assert driver.sessions == [{"database": "after-sales-knowledge", "default_access_mode": "READ"}]
    cypher, params = driver.calls[0]
    assert cypher == GRAPH_QUERY
    assert "$tenant_id" in cypher and "$release_id" in cypher and "$as_of" in cypher
    assert "$approved_paths" in cypher and "KnowledgeDocument" in cypher
    assert "ORDER BY a.name, type(r), b.name, r.evidence_id" in cypher
    assert params["approved_paths"] == [["Product", "COMPATIBLE_WITH", "Accessory"]]
    assert params["as_of"] == "2026-10-20"
    assert result.knowledge_release_id == RELEASE
    assert {node.node_type for node in result.nodes} == {"Product", "Accessory"}
    assert result.relations[0].evidence_id == result.evidence_refs[0] == "KNOW-EV-FILTER-01"
    assert result.evidence[0].document_id == "DOC-FILTER-F-01"


def test_untrusted_values_never_enter_cypher_and_relation_whitelist_rejects_unknown():
    driver = FakeDriver()
    hostile = "A' MATCH (n) DETACH DELETE n //"
    _search(driver, query=GraphQuery((hostile,), ("HAS_POLICY",), as_of=date(2026, 10, 20)))
    cypher, params = driver.calls[0]
    assert hostile not in cypher and params["entity_names"] == [hostile]
    with pytest.raises(ValueError, match="白名单"):
        _search(driver, query=GraphQuery(("A",), ("HAS_FAULT",)))
    with pytest.raises(ValueError, match="白名单"):
        _search(driver, query=GraphQuery(("A",), ("MATCH (n) RETURN n",)))
    assert len(driver.calls) == 1


def test_scope_empty_result_and_input_bounds():
    driver = FakeDriver()
    result = _search(driver, scope=GraphScope("tenant-north", RELEASE, frozenset()))
    assert result.relations == () and not driver.sessions
    with pytest.raises(ValueError, match="limit"):
        _search(driver, query=GraphQuery(("A",), ("HAS_POLICY",), limit=51))
    with pytest.raises(ValueError, match="租户"):
        _search(driver, scope=GraphScope("", RELEASE))


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row["b_props"].update(tenant_id="tenant-south"),
        lambda row: row["r_props"].update(knowledge_release_id="other-release"),
        lambda row: row["r_props"].update(evidence_id=""),
        lambda row: row["e_props"].update(evidence_id="other-evidence"),
        lambda row: row["r_props"].update(valid_to="2026-01-01"),
        lambda row: row["e_props"].update(status="draft"),
        lambda row: row.update(relation_type="ORDER_STATUS"),
    ],
)
def test_corrupt_or_out_of_scope_rows_fail_closed(change):
    row = deepcopy(_row())
    change(row)
    with pytest.raises(GraphIntegrityError):
        _search(FakeDriver([row]))


def test_authorized_node_scope_rechecked_after_database():
    row = _row()
    with pytest.raises(GraphIntegrityError, match="授权"):
        _search(
            FakeDriver([row]),
            scope=GraphScope("tenant-north", RELEASE, frozenset({"product-air-01"})),
        )


def test_expired_policy_and_mismatched_evidence_version_are_rejected():
    policy_row = deepcopy(_row("HAS_POLICY"))
    policy_row["b_props"]["effective_to"] = "2026-09-30"
    with pytest.raises(GraphIntegrityError, match="政策"):
        _search(
            FakeDriver([policy_row]),
            query=GraphQuery(("Air purifier A",), ("HAS_POLICY",), as_of=date(2026, 10, 20)),
        )
    row = deepcopy(_row())
    row["e_props"]["version"] = "2"
    with pytest.raises(GraphIntegrityError, match="版本"):
        _search(FakeDriver([row]))


def test_no_match_is_a_valid_empty_result():
    result = _search(FakeDriver())
    assert result.nodes == result.relations == result.evidence == ()
