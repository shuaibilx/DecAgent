# Disposable SOP Compose smoke and startup-race fix

Date: 2026-10-24 (Asia/Shanghai)

## Result

The isolated SOP Compose smoke passed after making Compose wait for service health before the script's first host-side API probe.

- Source: `edd30f1e60a10a21eebd3d41bced20d6bc8b4063` plus uncommitted worktree changes.
- Runtime: Windows 11 host, Docker Desktop Linux engine 29.8.2, Docker Compose v5.6.0, Python 3.11 from `D:\Anaconda3\envs\Agent311\python.exe`.
- Disposable Compose project: `decagent-sop-smoke-4eb3567a6e55`.
- Outcome: 6 services started; API and business readiness passed; API was bound to loopback; business MCP readiness passed; stopping the worker made the business probe fail while API liveness remained healthy; restarting the worker restored readiness.
- Model requests: 0. Guest trial: disabled. Docker socket: not mounted.
- Cleanup: the script removed the disposable containers, volumes, network, and image. A subsequent Docker query found no resource with the disposable project label or image tag.
- Existing `decagent` and PostgreSQL test containers remained running with their original names and host ports; the existing `decagent-api` stayed on `127.0.0.1:8000`.

## Root cause and correction

The first isolated run reached `docker compose up --detach`, which returns after containers start, then immediately issued one `GET /health`. The API container was still in Docker health state `starting`, and the connection closed before an HTTP response. The script's later bounded business-readiness loop could have retried the liveness probe, but the earlier one-shot request aborted first.

The smoke now starts the stack with Compose `--wait --wait-timeout 180`, using the health checks already defined for the API, Worker, and MCP services before probing from the host. The focused regression test failed before the helper existed and passes with the health-wait command contract.

The first command used the system Anaconda Python rather than the project's Python 3.11 environment and failed on `str.removeprefix` before calling Docker. It created no resources. The final run used the `Agent311` interpreter.

## Verification boundary

This verifies a disposable, locally built image and readiness/fault-recovery behavior. It does **not** complete OpenSpec task 4.4: no persistent first installation, failed-install recovery, GHCR upgrade, compatibility rollback, schema-incompatible stop, Windows login startup, Runner startup, or production data recovery was exercised. No release or CD workflow was dispatched.

## Automated checks

- `evals/deterministic/test_sop_smoke_diagnostics.py`: 6 passed.
- Full deterministic suite after the fix: 2,176 passed, 18 skipped in 255.47 seconds.
- Ruff: passed.
- OpenSpec strict validation: `jev-conditional-workflows` and `guest-sop-local-trial` passed.
- `git diff --check`: passed; Git emitted only configured LF-to-CRLF working-copy notices.

## Repeat on the current candidate (2026-10-24)

- Candidate source: `d7e8b0e79df5c443a756beb76f1094c799c28f1a`; the only pre-run worktree edit was OpenSpec task-status documentation.
- A second invocation of `D:\Anaconda3\envs\Agent311\python.exe -m ops.deploy.offline_sop_smoke` passed with disposable project `decagent-sop-smoke-dc9efb4eb833` and image tag `decagent-sop:offline-dc9efb4eb833`.
- The same six services reached readiness. Stopping and restarting the business Worker changed the readiness result to not-ready and back to ready. The script reported `model_requests=0`, `guest_trial=false`, and `docker_socket=false`.
- Script completion also verified removal of all containers, volumes, networks, and the uniquely tagged image. Existing application containers and volumes were not targeted.
- The real configuration preflight was rerun with `ops/deploy/sop.env`; it passed and reported API binding `127.0.0.1:8000`. A read-only Docker check confirmed the legacy `decagent-api` remains healthy and already publishes that same host binding. The static preflight does not detect host-port conflicts; persistent first install must resolve and verify the port plan before startup.
- This repeat still does not close task 4.4: persistent first install, same-digest recovery, upgrade, health rollback, schema-incompatible stop, Windows/Runner restart recovery, and backup restoration remain untested.
