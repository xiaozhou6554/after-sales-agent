"""mock-v2 场景执行器骨架（《项目开发流程》步骤05A）。

职责与边界：
- 每个用例执行前恢复种子（reset_to_seed），保证用例间彼此隔离；
- 校验用例结构与种子引用（dataset_version、租户、断言模式）；
- 登记 setup.overrides（语义控制面覆盖）与 setup.fault_injection（故障注入名），
  但不解释其语义——对应模块级 controls 尚未实现；
- 输出与 evaluate_scenario_run.cjs 契约兼容的结果行。

诚实性约束（流程 05A 明确要求）：Agent 用例与模块 controls 在步骤06+ 才实现，
当前所有用例一律输出 status=not_executed 并注明原因；不得按预期标签直接判通过。
后续步骤实现对应能力后，仅替换 run_case 的观察收集部分，行契约保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SCHEMA_VERSION = "2.0"
DATASET_VERSION = "mock-v2"
NOT_EXECUTED_REASON = "agent_use_cases_and_module_controls_not_implemented"

_REQUIRED_FIELDS = (
    "case_id",
    "dataset_version",
    "schema_version",
    "tenant_id",
    "partition",
    "setup",
    "expected",
)
_ALLOWED_PARTITIONS = ("development", "holdout")
_REQUIRED_ASSERTION_MODE = "structured_facts_tool_calls_effects_evidence_state"


class SeedManager(Protocol):
    """由 bootstrap.mock_data.SqlSeedManager 或测试替身实现。"""

    def reset_to_seed(self) -> dict[str, int]:
        """清空并重新导入种子，返回各表行数（用于隔离校验）。"""
        ...


class ScenarioExecutionError(RuntimeError):
    """用例结构或隔离性校验失败。"""


@dataclass(frozen=True)
class CaseRun:
    """单用例执行记录：结果行 + 登记信息。"""

    row: dict
    isolation_ok: bool
    overlays: list[dict] = field(default_factory=list)


def validate_case(case: dict, known_tenants: set[str]) -> list[str]:
    """结构校验，返回问题列表（空列表表示通过）。不抛异常，供逐例报告。"""
    problems: list[str] = []
    for name in _REQUIRED_FIELDS:
        if name not in case:
            problems.append(f"缺少必填字段 {name}")
    if case.get("dataset_version") != DATASET_VERSION:
        problems.append(
            f"dataset_version={case.get('dataset_version')!r} 与 {DATASET_VERSION!r} 不符"
        )
    if case.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version={case.get('schema_version')!r} 与 {SCHEMA_VERSION!r} 不符")
    if case.get("partition") not in _ALLOWED_PARTITIONS:
        problems.append(f"partition={case.get('partition')!r} 不在 {_ALLOWED_PARTITIONS} 内")
    if known_tenants and case.get("tenant_id") not in known_tenants:
        problems.append(f"tenant_id={case.get('tenant_id')!r} 不在种子租户表中")
    expected = case.get("expected")
    if isinstance(expected, dict) and expected.get("assertion_mode") != _REQUIRED_ASSERTION_MODE:
        problems.append(f"assertion_mode={expected.get('assertion_mode')!r} 不受支持")
    if not isinstance(case.get("setup"), dict):
        problems.append("setup 必须为对象")
    return problems


def run_case(case: dict, seed_manager: SeedManager, seed_counts: dict[str, int]) -> CaseRun:
    """恢复种子并登记覆盖后，产出 not_executed 结果行。

    隔离校验：reset_to_seed 返回的各表行数必须与种子预期完全一致，
    否则说明上一用例污染了数据或种子被改动，直接失败（fail-closed）。
    """
    actual_counts = seed_manager.reset_to_seed()
    mismatches = {
        table: {"expected": expected, "actual": actual_counts.get(table)}
        for table, expected in seed_counts.items()
        if actual_counts.get(table) != expected
    }
    if mismatches:
        raise ScenarioExecutionError(
            f"用例 {case.get('case_id')} 种子恢复后计数不一致: {mismatches}"
        )

    setup = case.get("setup", {})
    overlays = list(setup.get("overrides") or [])
    fault_injection = setup.get("fault_injection")
    row = {
        "case_id": case["case_id"],
        "dataset_version": case["dataset_version"],
        "partition": case.get("partition"),
        "status": "not_executed",
        "not_executed_reason": NOT_EXECUTED_REASON,
        "expected_schema_version": case.get("schema_version"),
        "expected_outcome_label": (case.get("expected") or {}).get("outcome_label"),
        "setup_applied": {
            "reset_to_seed": bool(setup.get("reset_to_seed")),
            "overlays": [overlay.get("overlay_id") for overlay in overlays],
            "fault_injection": fault_injection,
        },
        "observed": {
            "facts": {},
            "tool_calls": [],
            "workflow_steps": [],
            "effects": [],
            "evidence_refs": [],
            "forbidden_violations": [],
            "state": {},
        },
    }
    return CaseRun(row=row, isolation_ok=True, overlays=overlays)


def run_scenarios(
    cases: list[dict],
    seed_manager: SeedManager,
    seed_counts: dict[str, int],
    known_tenants: set[str],
) -> dict:
    """顺序执行全部用例，返回结果行列表与汇总。

    结构校验失败的用例同样输出 not_executed 行（附 validation_problems），
    不中断整体执行；隔离性失败属于致命状态，直接抛出。
    """
    rows: list[dict] = []
    validation_failures: list[dict] = []
    for case in cases:
        problems = validate_case(case, known_tenants)
        if problems:
            validation_failures.append({"case_id": case.get("case_id"), "problems": problems})
            rows.append(
                {
                    "case_id": case.get("case_id"),
                    "dataset_version": case.get("dataset_version"),
                    "partition": case.get("partition"),
                    "status": "not_executed",
                    "not_executed_reason": "case_validation_failed",
                    "validation_problems": problems,
                    "observed": None,
                }
            )
            continue
        run = run_case(case, seed_manager, seed_counts)
        rows.append(run.row)
    summary = {
        "dataset_version": DATASET_VERSION,
        "total_cases": len(cases),
        "not_executed": sum(1 for row in rows if row["status"] == "not_executed"),
        "validation_failures": validation_failures,
        "isolation": "verified_per_case" if not validation_failures else "partial",
    }
    return {"rows": rows, "summary": summary}
