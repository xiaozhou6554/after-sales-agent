"""Recover the current development Outbox backlog with the normal worker entry point."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from bootstrap.settings import load_settings
from sqlalchemy import text

from infrastructure.persistence.mysql.connection import create_mysql_engine

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evals/reports/2026-10-21-step35-outbox-recovery.json"


def status_counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        rows = connection.execute(
            text("SELECT status, COUNT(*) AS n FROM outbox_event GROUP BY status")
        ).mappings()
        return {str(row["status"]): int(row["n"]) for row in rows}


def pending_total(counts: dict[str, int]) -> int:
    return counts.get("pending", 0) + counts.get("publishing", 0)


def assert_no_other_worker() -> None:
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.info["pid"] == os.getpid():
            continue
        command = " ".join(process.info.get("cmdline") or [])
        if "bootstrap.worker_main" in command:
            raise RuntimeError(f"A worker is already running (pid={process.info['pid']})")


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production":
        raise RuntimeError("Refusing to start the worker in production")
    assert_no_other_worker()
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )
    worker = None
    started_at = datetime.now(timezone.utc)
    initial = status_counts(engine)
    if pending_total(initial) == 0:
        raise RuntimeError("No pending/publishing Outbox records to recover")

    try:
        worker = subprocess.Popen(
            [sys.executable, "-m", "bootstrap.worker_main"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        deadline = time.monotonic() + 180
        current = initial
        stable_zero_since: float | None = None
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                raise RuntimeError(f"Worker exited early with code {worker.returncode}")
            time.sleep(1)
            current = status_counts(engine)
            if pending_total(current) == 0:
                if stable_zero_since is None:
                    stable_zero_since = time.monotonic()
                elif time.monotonic() - stable_zero_since >= 5:
                    break
            else:
                stable_zero_since = None
        else:
            raise TimeoutError(f"Backlog did not drain in 180s; current states: {current}")
    finally:
        if worker is not None and worker.poll() is None:
            if os.name == "nt":
                worker.send_signal(signal.CTRL_C_EVENT)
            else:
                worker.send_signal(signal.SIGTERM)
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)

    final = status_counts(engine)
    engine.dispose()
    report = {
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "app_env": settings.app_env,
        "worker_entry_point": "python -m bootstrap.worker_main",
        "mock_handlers_only": True,
        "initial_outbox_status_counts": initial,
        "final_outbox_status_counts": final,
        "initial_pending_or_publishing": pending_total(initial),
        "remaining_pending_or_publishing": pending_total(final),
        "worker_exit_code": worker.returncode if worker is not None else None,
        "recovered": pending_total(final) == 0,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["recovered"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
