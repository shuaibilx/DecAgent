# Local SOP disposable Docker smoke

Date: 2026-10-23 (Asia/Shanghai)

## Result

The isolated SOP Compose smoke passed on the current local feature checkout (`HEAD` `0bdfe13e0db254411287bd4be2f086b8461d4d28`). The worktree was dirty, so this is local engineering evidence, not same-SHA CI or a published-release acceptance.

Command:

```powershell
.venv\Scripts\python.exe -m ops.deploy.offline_sop_smoke
```

The command exited 0 and reported `status=passed`. It built a uniquely tagged local image, started the disposable PostgreSQL, migration, bootstrap, business MCP, API, and business worker services, and verified API liveness, business readiness, internal MCP identity, conditional-workflow configuration, loopback-only API binding, and the absence of Docker-socket mounts. Worker fault injection left API liveness healthy while business readiness became false; restarting the worker restored readiness.

The smoke used generated temporary secrets and blank model keys, kept guest trial disabled, and reported zero model requests. It used its own random loopback port rather than the existing application's `127.0.0.1:8000`. The script's `finally` block ran Compose `down --volumes --remove-orphans` and checked for remaining project containers, volumes, networks, and image. Independent Docker queries after exit found none of those smoke artifacts.

The existing `decagent-api` service on port 8000 was left running and was not changed. This test does not complete task 4.4: it does not exercise persistent rollout state, backup/restore, release-image upgrade, compatible rollback, incompatible-schema stop, Windows login recovery, or private CD. A trusted SOP release image and same-SHA CI are still required before those checks.
