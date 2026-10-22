"""场景执行入口：生成 comparator 兼容的 mock-v2-run.jsonl（步骤05A）。

用法（项目根目录运行，须先完成迁移 v0002 与 python -m bootstrap.mock_data init）：
  python tests/harness/run_scenarios.py

当前阶段所有用例输出 status=not_executed（Agent 用例与模块 controls 在步骤06+ 实现），
因此 evaluate_scenario_run.cjs 的结论为 incomplete——这是当前阶段的正确结果，
不得据此或按预期标签宣称任何用例通过。

退出码：0 = 全部用例完成种子恢复且结构校验通过（均为 not_executed）；
1 = 隔离性失败或结构校验失败。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from bootstrap.mock_data import SqlSeedManager, load_seed_data  # noqa: E402
from bootstrap.settings import load_settings  # noqa: E402

from infrastructure.persistence.mysql.connection import create_mysql_engine  # noqa: E402
from infrastructure.persistence.mysql.mock_seed_importer import (  # noqa: E402
    expected_row_counts,
)
from tests.harness.scenario_executor import (  # noqa: E402
    ScenarioExecutionError,
    run_scenarios,
)

SCENARIOS_PATH = PROJECT_ROOT / "data" / "scenarios" / "v2" / "multi_system.jsonl"
OUTPUT_PATH = PROJECT_ROOT / "data" / "generated" / "runs" / "mock-v2-run.jsonl"
SUMMARY_PATH = PROJECT_ROOT / "data" / "generated" / "runs" / "mock-v2-run-summary.json"


def main() -> int:
    settings = load_settings()
    seed_data = load_seed_data()
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )

    cases = [
        json.loads(line)
        for line in SCENARIOS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seed_counts = expected_row_counts(seed_data)
    known_tenants = {row["tenant_id"] for row in seed_data["tenants"]}
    manager = SqlSeedManager(engine, seed_data)

    try:
        result = run_scenarios(cases, manager, seed_counts, known_tenants)
    except ScenarioExecutionError as exc:
        print(f"隔离性校验失败，已停止: {settings.mask_text(str(exc))}", flush=True)
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        for row in result["rows"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        **result["summary"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "output_file": OUTPUT_PATH.relative_to(PROJECT_ROOT).as_posix(),
        "note": "当前阶段全部用例 not_executed；真实执行与观察收集在后续步骤实现。",
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    ok = not summary["validation_failures"] and summary["not_executed"] == summary["total_cases"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
