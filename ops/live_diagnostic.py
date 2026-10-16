"""Explicit, bounded real-provider check using a dedicated persistent diagnostic ledger."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from uuid import uuid4

from langchain_openai import ChatOpenAI
from psycopg import sql
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.decisions import DecisionContext, JevAdapter, build_request
from decagent.models.primary import PrimaryModel
from decagent.policy import Redactor
from decagent.runtime.model_calls import BudgetedPrimary, LedgerAttemptBudget
from decagent.storage.database import Database
from decagent.storage.usage import UsageLedger


def run(settings, admin_url, budget_usd):
    if budget_usd <= 0 or budget_usd > 1:
        raise ValueError("This diagnostic is limited to the owner-approved one USD")
    state_path = settings.data_dir / "live-diagnostic.json"
    if state_path.exists():
        # Never restart paid checks merely because a previous process stopped.
        return json.loads(state_path.read_text(encoding="utf-8"))
    redactor = Redactor(
        known_secrets=(settings.main_api_key, settings.jev_api_key, settings.github_token)
    )
    administrator = Database(admin_url, redactor)
    name = "live_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(admin_url.rsplit("/", 1)[0] + "/" + name, redactor)
    database.migrate()
    repository = json.loads(
        (settings.data_dir / "github-test-repository.json").read_text(encoding="utf-8")
    )["repository"]
    project = database.add_project(repository)
    task = database.create_task(
        project["id"],
        "a" * 40,
        {"purpose": "Owner-authorized provider diagnostics; no project code transmitted"},
        {
            "max_calls": 2,
            "max_tokens": 40000,
            "max_cost": str(budget_usd),
            "max_active_seconds": 300,
            "allow_unknown_price": True,
        },
    )
    database.claim(task["id"], "live-diagnostic")
    report = {
        "status": "running",
        "database_name": name,
        "task_id": str(task["id"]),
        "budget_usd": str(budget_usd),
        "primary_model": settings.main_model,
        "jev_provider": settings.jev_provider,
        "checks": {},
    }

    def save():
        state_path.parent.mkdir(exist_ok=True)
        temp = state_path.with_suffix(".pending")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, state_path)

    save()  # Durable request intent precedes any paid network request.
    ledger = UsageLedger(database, task["id"])
    request = build_request(
        ["memory_read_needed"],
        {
            "task_summary": "A diagnostic asks whether reading project memory is necessary.",
            "current_version": "diagnostic-1",
        },
        DecisionContext(
            str(task["id"]), str(project["id"]), "diagnostic-1", "diagnostic-1", "1", "1"
        ),
    )
    with JevAdapter(
        api_key=settings.jev_api_key,
        model=settings.jev_model,
        provider=settings.jev_provider,
        max_retries=0,
        cache_ttl=0,
        budget=LedgerAttemptBudget(
            ledger, reservation_usd=0.01, provider="jev:" + settings.jev_provider
        ),
    ) as adapter:
        result = adapter.evaluate(request)
    report["checks"]["jev"] = result.to_dict()
    save()
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
        ).fetchall()
    held = sum(
        (
            r["actual_cost"]
            if r["status"] == "settled"
            else max(r["reserved_cost"] or Decimal(0), r["actual_cost"] or Decimal(0))
            for r in rows
        ),
        Decimal(0),
    )
    remaining = budget_usd - held
    if remaining > 0:
        # This provider supplies no published price in /models. Occupy all remaining
        # fees and allow only this one bounded diagnostic. No bill is inferred from tokens.
        client = ChatOpenAI(
            model=settings.main_model,
            base_url=settings.main_base_url,
            api_key=settings.main_api_key,
            use_responses_api=False,
            timeout=30,
            max_retries=0,
            max_tokens=256,
            stream_usage=True,
        )
        primary = BudgetedPrimary(
            PrimaryModel(client, redactor),
            ledger,
            max_output_tokens=256,
            unknown_cost_reservation=remaining,
        )
        try:
            turn = primary.respond(
                [
                    {
                        "role": "user",
                        "content": "Provider contract diagnostic. Propose exactly one read_file "
                        "tool call for "
                        "pyproject.toml, max_bytes 256. Do not execute it. No explanation needed.",
                    }
                ],
                stream=True,
            )
            report["checks"]["primary"] = {
                "status": "passed"
                if len(turn.tools) == 1 and turn.tools[0].name == "read_file"
                else "failed_tool_contract",
                "actual_model": turn.model,
                "stream_complete": True,
                "tool_calls": len(turn.tools),
                "usage": vars(turn.usage) if turn.usage else None,
                "cost_status": "unknown_provider_price; reservation retained",
            }
        except Exception as error:
            report["checks"]["primary"] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "cost_status": "unknown; reservation retained",
            }
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
        ).fetchall()
        conn.execute(
            "UPDATE app.tasks SET state='paused_budget',lease_until=NULL WHERE id=%s", (task["id"],)
        )
        conn.execute(
            "INSERT INTO app.decision_records(id,task_id,input_hash,question_version,"
            "policy_version,requested_model,resolved_model,result,final_route) "
            "VALUES(%s,%s,%s,'1','1',%s,%s,%s,'diagnostic-only')",
            (
                uuid4(),
                task["id"],
                request.input_hash,
                result.requested_model,
                result.actual_model,
                Jsonb(result.to_dict()),
            ),
        )
    report["status"] = "finished"
    report["usage_records"] = [
        {
            k: str(v) if isinstance(v, Decimal) else v
            for k, v in r.items()
            if k
            in {
                "provider",
                "requested_tokens",
                "actual_tokens",
                "reserved_cost",
                "actual_cost",
                "status",
            }
        }
        for r in rows
    ]
    save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-live", action="store_true", required=True)
    parser.add_argument("--budget-usd", type=Decimal, required=True)
    args = parser.parse_args()
    report = run(
        Settings.from_env(), os.environ["DECAGENT_DIAGNOSTIC_DATABASE_URL"], args.budget_usd
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": {
                    name: {
                        k: v
                        for k, v in item.items()
                        if k
                        in {
                            "status",
                            "actual_model",
                            "error",
                            "error_type",
                            "cost_status",
                            "stream_complete",
                            "tool_calls",
                            "usage",
                        }
                    }
                    for name, item in report["checks"].items()
                },
                "usage_records": report.get("usage_records", []),
            },
            ensure_ascii=False,
        )
    )
