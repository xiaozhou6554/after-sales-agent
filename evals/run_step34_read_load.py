"""Sustain authenticated read-only business API users for the SLO window."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from bootstrap.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def percentile(samples: list[float], quantile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return round(ordered[index], 3)


async def run(
    users: int,
    warmup_s: int,
    duration_s: int,
    think_time_s: float,
    output: Path,
) -> None:
    settings = load_settings()
    base_url = f"http://{settings.api_host}:{settings.api_port}"
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(timeout=10.0, limits=limits) as http:
        login = await http.post(
            f"{base_url}/auth/mock/session", json={"account_id": "cust-north-01"}
        )
        login.raise_for_status()
        session_token = login.json()["data"]["session_token"]
        run_id = uuid.uuid4().hex[:10]
        ready = 0
        all_ready = asyncio.Event()
        start = asyncio.Event()
        samples_ms: list[float] = []
        failures = 0
        warmup_requests = 0
        warmup_failures = 0
        status_counts: dict[str, int] = {}

        async def virtual_user() -> None:
            nonlocal ready, failures, warmup_requests, warmup_failures
            ready += 1
            if ready == users:
                all_ready.set()
            await start.wait()
            steady_start = benchmark_start + warmup_s
            deadline = steady_start + duration_s
            while time.monotonic() < deadline:
                measured = time.monotonic() >= steady_start
                request_started = time.perf_counter()
                status = "exception"
                try:
                    response = await http.get(
                        f"{base_url}/notifications",
                        params={"limit": 20},
                        headers={"Authorization": f"Bearer {session_token}"},
                    )
                    status = (
                        "success"
                        if response.status_code == 200
                        and response.json().get("status") == "success"
                        else f"http_or_contract_{response.status_code}"
                    )
                except Exception:  # noqa: BLE001 - count transport/protocol failures
                    status = "exception"
                if measured:
                    samples_ms.append((time.perf_counter() - request_started) * 1000)
                    status_counts[status] = status_counts.get(status, 0) + 1
                    if status != "success":
                        failures += 1
                else:
                    warmup_requests += 1
                    if status != "success":
                        warmup_failures += 1
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(think_time_s, remaining))

        tasks = [asyncio.create_task(virtual_user()) for _ in range(users)]
        await all_ready.wait()
        benchmark_start = time.monotonic()
        started_at = datetime.now(timezone.utc)
        start.set()
        await asyncio.gather(*tasks)
        finished_at = datetime.now(timezone.utc)

    successes = len(samples_ms) - failures
    report = {
        "run_id": run_id,
        "warmup_started_at_utc": started_at.isoformat(),
        "warmup_duration_seconds": warmup_s,
        "warmup_request_count": warmup_requests,
        "warmup_failure_count": warmup_failures,
        "started_at_utc": (started_at + timedelta(seconds=warmup_s)).isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "virtual_users": users,
        "duration_seconds": duration_s,
        "measured_duration_seconds": round(
            (finished_at - started_at).total_seconds() - warmup_s, 3
        ),
        "think_time_seconds": think_time_s,
        "operation": "authenticated API GET /notifications?limit=20 (read-only mock customer)",
        "request_count": len(samples_ms),
        "success_count": successes,
        "failure_count": failures,
        "availability": round(successes / len(samples_ms), 6) if samples_ms else 0,
        "latency_ms": {
            "p50": percentile(samples_ms, 0.50),
            "p95": percentile(samples_ms, 0.95),
            "p99": percentile(samples_ms, 0.99),
            "max": round(max(samples_ms), 3) if samples_ms else None,
        },
        "status_counts": status_counts,
        "targets": {
            "ordinary_read_p95_ms": 5000,
            "ordinary_read_p99_ms": 10000,
            "availability": 0.995,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--warmup-s", type=int, default=120)
    parser.add_argument("--duration-s", type=int, default=1800)
    parser.add_argument("--think-time-s", type=float, default=30.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evals/reports/2026-10-21-step34-read-load.json",
    )
    args = parser.parse_args()
    if args.users <= 0 or args.warmup_s < 0 or args.duration_s <= 0 or args.think_time_s < 0:
        parser.error(
            "users and duration must be positive; warmup and think-time cannot be negative"
        )
    asyncio.run(run(args.users, args.warmup_s, args.duration_s, args.think_time_s, args.output))


if __name__ == "__main__":
    main()
