"""Local administration and authenticated task control entrypoints."""

import argparse
import json
import os
from pathlib import Path

from decagent import __version__
from decagent.config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decagent")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local configuration without paid model calls")
    readiness = commands.add_parser(
        "model-readiness", help="Run the isolated, capped Qwen/OpenRouter readiness diagnostic"
    )
    readiness.add_argument("--live", action="store_true", help="Dispatch the bounded probe")
    readiness.add_argument("--resume-known-not-sent", action="store_true")
    readiness.add_argument(
        "--ledger", default=".decagent/routed-model-readiness-v3-20261024.sqlite"
    )
    readiness.add_argument(
        "--cohort-profiles",
        default="evals/benchmarks/manifests/evaluation_model_profiles_v7.json",
    )
    readiness.add_argument("--evidence")
    commands.add_parser("migrate", help="Apply versioned database migrations")
    commands.add_parser("init", help="Initialize the owner credential in the local data directory")
    serve = commands.add_parser("serve", help="Start the authenticated workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    business_server = commands.add_parser(
        "business-server", help="Start the employee-authenticated business MCP server"
    )
    business_server.add_argument("--host", default="127.0.0.1")
    business_server.add_argument("--port", type=int)
    business_server.add_argument("--resource-url")
    worker = commands.add_parser("worker", help="Run the serial task worker")
    worker.add_argument("--kind", choices=("business", "repository_repair"))
    from decagent.business.administration import add_commands

    add_commands(commands)
    commands.add_parser("tasks", help="List tasks through the owner API")
    intervention = commands.add_parser(
        "intervention-report",
        help="Export intervention scenarios or an unlabeled task trace without paid requests",
    )
    intervention.add_argument("--task-id")
    intervention.add_argument("--root", default=".")
    release = commands.add_parser("release-report", help="Check saved evidence without model calls")
    release.add_argument("manifest")
    release.add_argument("--root", default=".")
    task = commands.add_parser("task", help="Submit, inspect or control a task")
    actions = task.add_subparsers(dest="task_action", required=True)
    submit = actions.add_parser("submit")
    submit.add_argument("--project-id", required=True)
    submit.add_argument("--base-ref", required=True)
    submit.add_argument("--issue", required=True)
    submit.add_argument("--publish-draft", action="store_true")
    submit.add_argument("--trust", choices=("exact_commit", "own_branch"))
    submit.add_argument("--branch")
    for name in ("show", "cancel", "resume"):
        action = actions.add_parser(name)
        action.add_argument("task_id")
        if name == "resume":
            resume = action
    for target in (submit, resume):
        target.add_argument("--max-cost")
        target.add_argument("--max-calls", type=int)
        target.add_argument("--max-tokens", type=int)
        target.add_argument("--max-active-seconds", type=int)
        target.add_argument("--allow-unknown-price", action="store_true")
    backup = commands.add_parser("backup", help="Export a snapshot to a new directory")
    backup.add_argument("destination")
    restore = commands.add_parser(
        "restore", help="Restore to a new empty database and evidence volume"
    )
    restore.add_argument("source")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.command == "model-readiness":
        from decagent.evaluation.business.routed_readiness import main as readiness_main

        forwarded = [
            "--ledger", args.ledger,
            "--cohort-profiles", args.cohort_profiles,
        ]
        if args.live:
            forwarded.append("--live")
        if args.resume_known_not_sent:
            forwarded.append("--resume-known-not-sent")
        if args.evidence:
            forwarded.extend(("--evidence", args.evidence))
        return readiness_main(forwarded)
    if args.command in {"plugins", "business", "seed-tickets"}:
        from decagent.business.administration import run_command

        run_command(settings, args)
    elif args.command == "intervention-report":
        from decagent.evaluation.intervention import export_report, scenario_report, trace_report

        if args.task_id:
            from uuid import UUID

            from decagent.gateway import OwnerClient

            identifier = str(UUID(args.task_id))
            with OwnerClient(settings.public_url, settings.data_dir / "owner.token") as client:
                report = trace_report(client.task(identifier))
        else:
            report = scenario_report()
        paths = export_report(args.root, report)
        print(
            json.dumps(
                {
                    "source": report["source"],
                    "paths": {k: str(v) for k, v in paths.items()},
                    "paid_requests": 0,
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "release-report":
        from decagent.evaluation.release import export_release_report, load_and_assess

        report = load_and_assess(args.manifest, args.root)
        path = export_release_report(args.root, report)
        print(json.dumps({"status": report["status"], "report": str(path)}, ensure_ascii=False))
        if report["status"] != "passed":
            raise SystemExit(1)
    elif args.command == "init":
        from decagent.gateway import initialize_owner
        from decagent.storage.database import Database

        database = Database(settings.database_url)
        database.migrate()
        path = initialize_owner(database, settings.data_dir / "owner.token")
        print(f"Owner credential saved to {path}; keep this file private")
    elif args.command == "serve":
        import uvicorn

        from decagent.runtime.application import create_workbench

        uvicorn.run(
            create_workbench(settings),
            host=args.host,
            port=args.port,
            proxy_headers=False,
        )
    elif args.command == "business-server":
        import uvicorn

        from decagent.business.employee_auth import EmployeeAuth
        from decagent.business.mcp_server import create_mcp_app
        from decagent.gateway.auth import OwnerAuth
        from decagent.storage.database import Database

        database = Database(settings.database_url)
        owner_auth = OwnerAuth(database, settings.public_url)
        employee_auth = EmployeeAuth(
            database, owner_auth.origin, owner_auth.secure, settings.data_dir / "demo-auth"
        )
        port = (
            args.port
            if args.port is not None
            else int(os.getenv("DECAGENT_LOCAL_MCP_PORT", "8001"))
        )
        mcp_options = {"host": args.host, "port": port}
        if args.resource_url is not None:
            mcp_options["resource_url"] = args.resource_url
        app = create_mcp_app(database, employee_auth, **mcp_options)
        uvicorn.run(app, host=args.host, port=port)
    elif args.command == "worker":
        from decagent.runtime.worker import run_worker

        run_worker(settings, kind=args.kind)
    elif args.command in {"task", "tasks"}:
        from decagent.gateway import OwnerClient

        with OwnerClient(settings.public_url, settings.data_dir / "owner.token") as client:
            if args.command == "tasks":
                result = client.tasks()
            elif args.task_action == "show":
                result = client.task(args.task_id)
            elif args.task_action == "cancel":
                result = client.cancel(args.task_id)
            else:
                budget = {
                    key: getattr(args, key)
                    for key in ("max_cost", "max_calls", "max_tokens", "max_active_seconds")
                    if getattr(args, key) is not None
                }
                if args.allow_unknown_price:
                    budget["allow_unknown_price"] = True
                if args.task_action == "resume":
                    if budget:
                        prior = client.task(args.task_id)["budget"]
                        budget = {**prior, **budget}
                    result = client.resume(args.task_id, budget=budget or None)
                else:
                    body = {
                        "project_id": args.project_id,
                        "base_ref": args.base_ref,
                        "issue": args.issue,
                        "publish_draft_pr": args.publish_draft,
                        "budget": budget,
                        "trust_scope": (
                            {"kind": args.trust, "branch": args.branch} if args.trust else None
                        ),
                    }
                    result = client.request("POST", "/tasks", body=body)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "migrate":
        from decagent.storage.database import Database

        Database(settings.database_url).migrate()
        print("Database migration complete")
    elif args.command in {"backup", "restore"}:
        from decagent.storage.backup import export_backup, restore_backup
        from decagent.storage.database import Database
        from decagent.storage.evidence import EvidenceStore

        database = Database(settings.database_url)
        if args.command == "backup":
            manifest = export_backup(
                database,
                EvidenceStore(settings.data_dir / "evidence"),
                Path(args.destination),
            )
        else:
            manifest = restore_backup(database, Path(args.source), settings.data_dir / "evidence")
        print(json.dumps(manifest["counts"], indent=2))
    elif args.command == "doctor":
        print(
            json.dumps(
                {
                    "python_package": __version__,
                    "database_configured": bool(settings.database_url),
                    "main_model_configured": bool(settings.main_api_key),
                    "jev_configured": bool(settings.jev_api_key),
                    "github_configured": bool(settings.github_token),
                    "paid_requests": 0,
                },
                indent=2,
            )
        )
