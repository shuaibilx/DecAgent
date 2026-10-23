# Local SOP Configuration and Compose Preflight

Date: 2026-10-23 (Asia/Shanghai)

## Outcome

The local SOP configuration is prepared and the static Compose preflight passed. No container was started, no model/API request was made, and no GitHub repository setting was changed.

## Local preparation

- `ops/deploy/sop.env` was created from `ops/deploy/sop.env.example`. The Qwen fallback and guest trial switches remain `false`; the API bind remains `127.0.0.1:8000`.
- `.decagent/sop-secrets/postgres_password` contains a newly generated random password. `main_api_key` and `jev_api_key` were copied locally from the existing ignored root `.env`; values were not printed, hashed into this report, or written to Git.
- Windows ACLs on the secret directory and its three files restrict access to the current Windows account. `.env`, `ops/deploy/sop.env`, and `.decagent/` are ignored by Git.
- The existing root `.env` was not modified.

## Environment check

- Docker reported `linux` as its engine OS type.
- Docker Compose reported `v5.6.0`.
- The preflight command was run with the project's `Agent311` interpreter:

```powershell
D:\Anaconda3\envs\Agent311\python.exe -m ops.deploy.sop_preflight `
  --project-directory . `
  --compose-file compose.sop.yaml `
  --env-file ops/deploy/sop.env
```

The command returned exit code 0 and a summary with `status=passed`, project `decagent-sop`, services `api`, `bootstrap`, `business-mcp`, `migrate`, `postgres`, `worker`, API binding `127.0.0.1:8000 -> 8000`, `business_readiness=true`, `docker_socket=false`, and `guest_trial=false`. The readiness field here means the required healthcheck is configured in the rendered Compose contract; it does not claim a running container is healthy.

## Still pending

- No release candidate control-image digest is available for the current SOP changes, and no release tag was created.
- The GitHub App and its repository-scoped installation are not configured; both repositories still report zero self-hosted runners.
- No service, database, Docker volume, network, or Runner was started by this preflight.
- Real first install, backup/recovery, normal upgrade, compatible health-failure rollback, schema-incompatible stop, Windows login recovery, and private CD remain OpenSpec tasks 4.4, 5.3, and 5.4.

The earlier [private deployment boundary snapshot](private-deployment-boundary-20261023.md) predates this local preparation and remains a point-in-time account/Runner check. This report supersedes only its statement that local SOP env and secret files were absent; it does not supersede the missing GitHub App, Runner, or real deployment findings.
