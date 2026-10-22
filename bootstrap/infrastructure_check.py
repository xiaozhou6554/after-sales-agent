"""按需执行基础依赖连接探测（运维诊断入口，不随进程启动自动运行）。

逐项探测 MySQL/Redis/Neo4j/RabbitMQ，输出脱敏结果与耗时；任何一项故障时
退出码为 1。错误信息经 settings.mask_text 掩码，不泄露连接密钥。
"""

from __future__ import annotations

import argparse
import sys

from bootstrap.container import build_runtime_container
from bootstrap.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基础依赖连接探测")
    parser.parse_args(argv)
    settings = load_settings()
    container = build_runtime_container(settings)
    try:
        results = container.run_all_probes()
    finally:
        container.close()

    print(f"基础依赖探测（app_env={settings.app_env}）：")
    failed = False
    for result in results:
        state = "up" if result.ok else "down"
        line = f"  {result.name:<10} {state}  {result.latency_ms}ms"
        if not result.ok:
            failed = True
            error = result.error or "unknown"
            line += f"  error={settings.mask_text(error)}"
        print(line)
    if failed:
        print("存在不可用依赖。")
        return 1
    print("全部基础依赖可连接。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
