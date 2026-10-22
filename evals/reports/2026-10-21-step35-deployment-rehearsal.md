# Step 35 deployment rehearsal

Date: 2026-10-21

## Build and static checks

- Built the API, MCP, worker, and migration images from the same Linux/Python 3.10 container lock. The lock pins 157 Python distributions, including CPU-only PyTorch `2.14.0+cpu`; the project lock was not changed.
- `docker run --rm --entrypoint python agent-api -m pip check`: passed, no broken requirements.
- Imported `bootstrap.api_main`, `bootstrap.mcp_main`, and `bootstrap.worker_main` inside the image: passed.
- `docker compose config --quiet` and `docker compose --profile app --profile migrate config --quiet`: passed. The base compose service list remains MySQL, Redis, RabbitMQ, and Neo4j; app and migration services are opt-in profiles.

## Runtime rehearsal

- Before starting a worker, read-only database counts found 42 pending `outbox_event` records and no `agent_task` rows. The worker was therefore not started or restarted, to avoid consuming existing business events.
- API and MCP were started on isolated host ports 18000 and 18001. Their `/health/live` endpoints returned HTTP 200 and both containers reached `healthy`.
- The first MCP startup exposed an unset `ADDRESS_ENCRYPT_KEY`. A one-time rehearsal value was passed only to the Compose process; neither `.env` nor persistent environment settings were edited. After a successful API/MCP restart, both live endpoints again returned HTTP 200.
- The rehearsal containers were removed afterward so the temporary key is not retained in container metadata. Existing database/queue infrastructure and the pending outbox records were left running and unchanged.

## Limits

- Worker startup/recovery was intentionally not exercised because 42 pending outbox events exist. Verify their intended handling before starting the worker in this shared development environment.
- Production secrets, TLS, external ingress, backups, deployment orchestration, and production recovery were not validated.
- `.env.example` now includes the required `ADDRESS_ENCRYPT_KEY` entry and instructs operators to set a private value before starting the app profile. Existing `.env` files must be edited selectively; do not replace them with the example file.

## Development backlog recovery follow-up (2026-10-21)

After the full regression run, a read-only status query found 50 pending events (the earlier 42 count above was the pre-regression snapshot). Event types and the worker path were inspected first. `APP_ENV=development`; refund review and notification sending are explicitly Mock handlers in this environment. No pre-existing worker process was found.

The project worker entry point `python -m bootstrap.worker_main` was run through `evals/recover_step35_outbox.py`. It drained the initial 50 pending records and published follow-on events emitted by the Mock refund and notification handlers, for 72 published events total. Final status counts were 72 `published`, 0 `pending`/`publishing`; the backlog remained at zero for the five-second stability window. The first Windows process stop used `terminate()` and returned exit code 1 after draining; subsequent `CTRL_C_EVENT` stops also returned exit code 1 after draining. The recovery metric was the database state held at zero for five seconds, and is recorded separately from the process exit code. This is a development Mock recovery result, not evidence of production worker recovery.

An idle worker was also started after the backlog was empty and stopped with the same Windows control event. It returned exit code 1 with no traceback/error lines captured; database status remained `published: 68`, with no pending Outbox or Agent tasks. The non-zero Windows stop result remains an operational follow-up and is not hidden by the successful drain metric.

## Final full-suite backlog recovery follow-up (2026-10-21)

The step36 final regression completed with 728 passed, 0 failed, and 31 third-party deprecation warnings. It left 13 development Outbox events from address-change, warehouse-return, and notification integration cases. After checking their event types and timestamps, the normal development worker was run with Mock handlers only. It published those events and their follow-ons: 13 pending became 68 published, with 0 pending/publishing held for the five-second stability window. The worker process again returned exit code 1 after the backlog was drained; this shutdown result is retained rather than reported as a clean exit. Machine-readable evidence is `evals/reports/2026-10-21-step35-outbox-recovery-after-full-suite.json`.

A preceding post-targeted-test recovery is preserved in `evals/reports/2026-10-21-step35-outbox-recovery-followup.json` (9 pending to 64 published, zero pending/publishing). These are local development Mock recoveries and do not establish production worker recovery.
