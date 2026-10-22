"""步骤05A 单元测试：场景执行器骨架（校验、隔离、not_executed 行契约）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harness.scenario_executor import (
    NOT_EXECUTED_REASON,
    ScenarioExecutionError,
    run_case,
    run_scenarios,
    validate_case,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = PROJECT_ROOT / "data" / "scenarios" / "v2" / "multi_system.jsonl"

SEED_COUNTS = {"mock_sys_tenant": 2, "mock_oms_order": 1005}
KNOWN_TENANTS = {"tenant-north", "tenant-south"}


def _load_first_case() -> dict:
    line = SCENARIOS_PATH.read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


class FakeSeedManager:
    """测试替身：记录重置次数，可注入污染的行数以验证隔离校验。"""

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.calls = 0
        self.counts = counts if counts is not None else dict(SEED_COUNTS)

    def reset_to_seed(self) -> dict[str, int]:
        self.calls += 1
        return dict(self.counts)


def test_real_dataset_first_case_passes_validation() -> None:
    case = _load_first_case()
    assert validate_case(case, {"tenant-north", "tenant-south"}) == []


def test_validate_case_reports_each_problem() -> None:
    problems = validate_case(
        {
            "case_id": "x",
            "dataset_version": "mock-v1",
            "schema_version": "1.0",
            "tenant_id": "tenant-unknown",
            "partition": "staging",
            "expected": {"assertion_mode": "eyeball"},
            "setup": "not-a-dict",
        },
        KNOWN_TENANTS,
    )
    joined = "\n".join(problems)
    assert "dataset_version" in joined
    assert "schema_version" in joined
    assert "partition" in joined
    assert "tenant_id" in joined
    assert "assertion_mode" in joined
    assert "setup" in joined


def test_run_case_row_matches_comparator_contract() -> None:
    case = _load_first_case()
    manager = FakeSeedManager()
    run = run_case(case, manager, SEED_COUNTS)
    row = run.row
    # 比较器必读字段
    assert row["case_id"] == case["case_id"]
    assert row["dataset_version"] == "mock-v2"
    assert row["status"] == "not_executed"
    assert row["expected_schema_version"] == "2.0"
    observed = row["observed"]
    for key in (
        "facts",
        "tool_calls",
        "workflow_steps",
        "effects",
        "evidence_refs",
        "forbidden_violations",
        "state",
    ):
        assert key in observed
    assert observed["facts"] == {} and observed["tool_calls"] == []
    # 登记信息：不得虚报为已执行，必须注明原因
    assert row["not_executed_reason"] == NOT_EXECUTED_REASON
    assert run.isolation_ok is True
    assert manager.calls == 1


def test_run_case_records_overlays_and_fault_injection() -> None:
    case = _load_first_case()
    case["setup"]["overrides"] = [
        {
            "tenant_id": "tenant-north",
            "overlay_id": "overlay-unmapped",
            "changes": {"mapping.status": "unmapped"},
        }
    ]
    case["setup"]["fault_injection"] = "commit_then_timeout"
    run = run_case(case, FakeSeedManager(), SEED_COUNTS)
    assert run.row["setup_applied"]["overlays"] == ["overlay-unmapped"]
    assert run.row["setup_applied"]["fault_injection"] == "commit_then_timeout"
    # overrides 只登记不解释：不影响 status，仍为 not_executed
    assert run.row["status"] == "not_executed"


def test_isolation_mismatch_fails_closed() -> None:
    case = _load_first_case()
    polluted = FakeSeedManager(counts={"mock_sys_tenant": 2, "mock_oms_order": 1004})
    with pytest.raises(ScenarioExecutionError, match="mock_oms_order"):
        run_case(case, polluted, SEED_COUNTS)


def test_run_scenarios_all_not_executed_with_summary() -> None:
    first = _load_first_case()
    broken = {**first, "case_id": "broken-case", "dataset_version": "mock-v1"}
    result = run_scenarios([first, broken], FakeSeedManager(), SEED_COUNTS, KNOWN_TENANTS)
    summary = result["summary"]
    assert summary["total_cases"] == 2
    assert summary["not_executed"] == 2
    assert summary["validation_failures"][0]["case_id"] == "broken-case"
    row_ok, row_bad = result["rows"]
    assert row_ok["status"] == "not_executed"
    assert row_bad["status"] == "not_executed"
    assert row_bad["not_executed_reason"] == "case_validation_failed"
    assert row_bad["validation_problems"]
    assert "observed" in row_ok


def test_full_real_dataset_runs_and_all_not_executed() -> None:
    """真实 149 例全量跑一遍骨架（FakeSeedManager，不触数据库）：
    每例都完成一次种子恢复且全部为 not_executed。"""
    cases = [
        json.loads(line)
        for line in SCENARIOS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manager = FakeSeedManager()
    result = run_scenarios(cases, manager, {"mock_sys_tenant": 2}, KNOWN_TENANTS)
    assert manager.calls == len(cases) == 149
    assert result["summary"]["not_executed"] == 149
    assert result["summary"]["validation_failures"] == []
    assert result["summary"]["isolation"] == "verified_per_case"
