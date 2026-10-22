"""模拟数据初始化/重置/校验 CLI（《项目开发流程》步骤05A）。

用法（项目根目录运行）：
  python -m bootstrap.mock_data init    # 清空 mock 表并按种子导入，随后校验
  python -m bootstrap.mock_data reset   # 仅清空 mock 表
  python -m bootstrap.mock_data verify  # 核对各表行数与种子/清单一致
  python -m bootstrap.mock_data status  # 输出各表当前行数

- 种子：data/mock/v2/systems.json（确定性生成器产物，不手工编辑）；
- 清单：data/manifests/mock-v2.json（business_record_counts 交叉核对）；
- 执行报告写入 data/generated/reports/mock-v2-seeding.json；
- 表结构由迁移 v0002 创建，须先执行 python -m bootstrap.migrate apply；
- 初始化与重置可重复执行；任何进程启动流程不调用本模块。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from bootstrap.settings import load_settings
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.mock_seed_importer import (
    DATASET_VERSION,
    TABLE_SPECS,
    compare_counts,
    expected_row_counts,
    import_mock_seed,
    reset_mock_tables,
    table_counts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMS_PATH = PROJECT_ROOT / "data" / "mock" / "v2" / "systems.json"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "mock-v2.json"
REPORT_PATH = PROJECT_ROOT / "data" / "generated" / "reports" / "mock-v2-seeding.json"


class SeedManager(Protocol):
    """场景执行器依赖的最小种子管理接口（每例恢复种子）。"""

    def reset_to_seed(self) -> dict[str, int]:
        """清空并重新导入种子，返回各表行数。"""
        ...


class SqlSeedManager:
    """基于 MySQL 的种子管理实现：清空 + 事务内整体导入。"""

    def __init__(self, engine, seed_data: dict) -> None:
        self._engine = engine
        self._seed_data = seed_data

    def reset_to_seed(self) -> dict[str, int]:
        with self._engine.begin() as conn:
            reset_mock_tables(conn)
        with self._engine.begin() as conn:
            return import_mock_seed(conn, self._seed_data)


def load_seed_data() -> dict:
    if not SYSTEMS_PATH.exists():
        raise FileNotFoundError(f"种子文件不存在: {SYSTEMS_PATH.relative_to(PROJECT_ROOT)}")
    with SYSTEMS_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _manifest_mismatches(data: dict) -> list[str]:
    """种子数组长度与版本清单 business_record_counts 交叉核对。"""
    if not MANIFEST_PATH.exists():
        return [f"清单文件不存在: {MANIFEST_PATH.relative_to(PROJECT_ROOT)}"]
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("dataset_version") != DATASET_VERSION:
        return [
            f"清单 dataset_version={manifest.get('dataset_version')!r} 与 {DATASET_VERSION!r} 不符"
        ]
    declared = manifest.get("counts", {}).get("business_record_counts", {})
    mismatches = []
    for spec in TABLE_SPECS:
        if spec.array_key == "tenants":
            continue
        actual = len(data[spec.array_key])
        if declared.get(spec.array_key) != actual:
            mismatches.append(
                f"{spec.array_key}: manifest={declared.get(spec.array_key)} seed={actual}"
            )
    return mismatches


def _write_report(
    action: str,
    expected: dict[str, int],
    actual: dict[str, int],
    manifest_mismatches: list[str],
    ok: bool,
) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "dataset_version": DATASET_VERSION,
        "action": action,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": "passed" if ok else "failed",
        "manifest_check": "passed" if not manifest_mismatches else "failed",
        "manifest_mismatches": manifest_mismatches,
        "per_table": {
            table: {"expected": expected.get(table), "actual": actual.get(table)}
            for table in expected
        },
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _print_counts(counts: dict[str, int]) -> None:
    for table, count in counts.items():
        print(f"{table}\t{count}")


def run_init(engine, data: dict, log) -> int:
    manager = SqlSeedManager(engine, data)
    manager.reset_to_seed()
    log("导入完成，开始校验……")
    return _verify(engine, data, "init", log)


def run_reset(engine, log) -> int:
    with engine.begin() as conn:
        reset_mock_tables(conn)
    log("已清空全部 mock 表。")
    return 0


def _verify(engine, data: dict, action: str, log) -> int:
    expected = expected_row_counts(data)
    with engine.connect() as conn:
        actual = table_counts(conn)
    mismatches = compare_counts(expected, actual)
    manifest_mismatches = _manifest_mismatches(data)
    ok = not mismatches and not manifest_mismatches
    _write_report(action, expected, actual, manifest_mismatches, ok)
    if mismatches:
        log("表计数与种子不一致：")
        for item in mismatches:
            log(f"  {item}")
    if manifest_mismatches:
        log("种子与版本清单不一致：")
        for item in manifest_mismatches:
            log(f"  {item}")
    total_actual = sum(actual.values())
    total_expected = sum(expected.values())
    log(
        f"校验{'通过' if ok else '失败'}：共 {total_actual} 行（预期 {total_expected} 行）；"
        f"报告已写入 {REPORT_PATH.relative_to(PROJECT_ROOT).as_posix()}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mock-v2 模拟数据初始化与校验")
    parser.add_argument("action", choices=["init", "reset", "verify", "status"])
    args = parser.parse_args(argv)

    settings = load_settings()
    data = load_seed_data()
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )

    def log(message: str) -> None:
        print(message, flush=True)

    try:
        if args.action == "status":
            with engine.connect() as conn:
                _print_counts(table_counts(conn))
            return 0
        if args.action == "reset":
            return run_reset(engine, log)
        if args.action == "verify":
            return _verify(engine, data, "verify", log)
        return run_init(engine, data, log)
    except Exception as exc:  # CLI 边界：掩码后输出，不泄露凭据
        log(f"执行失败: {settings.mask_text(str(exc))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
