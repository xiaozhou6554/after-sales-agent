"""步骤19：固定语料导入前校验与幂等写入形状。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from infrastructure.graph.schema import SCHEMA_STATEMENTS
from infrastructure.graph.seed import _CORPUS, _MANIFEST, build_seed_plan, seed_reviewed_graph
from tests.unit.test_step19_graph import FakeDriver

RELEASE = "KNOWLEDGE-RELEASE-2026-10"


class BytesPath:
    def __init__(self, value):
        self.value = value

    def read_bytes(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")


class TextPath:
    def __init__(self, value):
        self.value = value

    def read_text(self, **_):
        return json.dumps(self.value)


def _mutated_plan(change):
    corpus = json.loads(_CORPUS.read_text(encoding="utf-8"))
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    change(corpus, manifest)
    data = BytesPath(corpus)
    manifest["files"]["data/knowledge/v2/corpus.json"] = hashlib.sha256(
        data.read_bytes()
    ).hexdigest()
    return build_seed_plan(data, TextPath(manifest), "tenant-north", RELEASE)


def test_reviewed_graph_is_scoped_and_all_edges_have_published_evidence():
    north = build_seed_plan(_CORPUS, _MANIFEST, "tenant-north", RELEASE)
    south = build_seed_plan(_CORPUS, _MANIFEST, "tenant-south", RELEASE)
    assert tuple(map(len, north)) == (7, 7, 6)
    assert tuple(map(len, south)) == (2, 1, 1)
    assert all(x["props"]["tenant_id"] == "tenant-north" for x in north[0])
    assert {e["relation"] for e in north[2]} >= {"COMPATIBLE_WITH", "HAS_POLICY"}
    assert all(e["props"]["evidence_id"] for e in north[2])


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda c, m: c["graph"]["edges"][0].update(evidence_id="missing"), "证据"),
        (lambda c, m: c["graph"]["edges"][0].update(relation="ORDER_STATUS"), "白名单|类型"),
        (lambda c, m: c["graph"]["edges"][0].update(valid_to="2025-01-01"), "有效期"),
        (lambda c, m: c["graph"]["edges"][0].update(version="wrong"), "版本"),
        (lambda c, m: c["graph"]["nodes"][5].update(version=3), "政策版本"),
        (lambda c, m: c["documents"][5].update(content="tampered"), "清单"),
    ],
)
def test_bad_graph_cannot_reach_write_phase(change, match):
    with pytest.raises(ValueError, match=match):
        _mutated_plan(change)


def test_seed_uses_fixed_merge_templates_and_same_keys_on_replay():
    driver = FakeDriver()
    first = seed_reviewed_graph(driver, "after-sales-knowledge", "tenant-north", RELEASE)
    initial = deepcopy(driver.calls)
    second = seed_reviewed_graph(driver, "after-sales-knowledge", "tenant-north", RELEASE)
    assert first == second == (14, 6)
    assert driver.calls[: len(initial)] == driver.calls[len(initial) :]
    assert len(driver.calls) == 2 * (len(SCHEMA_STATEMENTS) + 14 + 6)
    assert [query for query, _ in initial[: len(SCHEMA_STATEMENTS)]] == list(SCHEMA_STATEMENTS)
    writes = initial[len(SCHEMA_STATEMENTS) :]
    assert all("MERGE" in cypher and "DELETE" not in cypher for cypher, _ in writes)
    assert all(
        params["tenant_id"] == "tenant-north" and params["release_id"] == RELEASE
        for _, params in writes
    )


def test_bad_release_rejected_before_session():
    driver = FakeDriver()
    with pytest.raises(ValueError, match="清单"):
        seed_reviewed_graph(driver, "after-sales-knowledge", "tenant-north", "other-release")
    assert not driver.sessions
