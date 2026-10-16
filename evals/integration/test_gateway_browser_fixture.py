"""Run a local real-PostgreSQL browser fixture. No model, Docker or GitHub calls.

This is an explicitly labelled offline UI fixture, not an end-to-end repair claim.
Run: python -m evals.integration.test_gateway_browser_fixture
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import uvicorn
from psycopg import sql
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from decagent.gateway.auth import token_hash
from decagent.memory import MemoryManager
from decagent.policy import (
    Action,
    ApprovalKind,
    CodeOrigin,
    CodeSnapshot,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
    action_digest,
)
from decagent.policy.approvals import confirmation_digest
from decagent.policy.canonical import primitive
from decagent.runtime.intervention import help_packet
from decagent.runtime.progress import observe_progress, route_progress
from decagent.storage.database import Database
from decagent.storage.evidence import EvidenceStore

# Public fixture credential, usable only for the newly created local fixture database.
FIXTURE_TOKEN = "dca_" + "offline-browser-fixture-" * 3


def build_fixture():
    url = os.environ["DECAGENT_TEST_DATABASE_URL"]
    admin = Database(url)
    name = "browser_" + uuid4().hex
    with admin.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    root = Path("output/playwright/browser-fixture").resolve()
    root.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceStore(root / "evidence")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.owner_credentials(token_hash) VALUES(%s)", (token_hash(FIXTURE_TOKEN),)
        )
    project = database.add_project("fixture/browser-workbench")

    def context(task_id):
        task = database.get_task(task_id)
        saved = task["authorization_snapshot"]
        provenance = Provenance(Source.OWNER, "owner", "browser-offline-fixture", True)
        authorization = OwnerAuthorization(
            saved["id"],
            saved["version"],
            str(task["id"]),
            str(project["id"]),
            project["repository"],
            provenance,
        )
        return PolicyContext(
            str(task["id"]),
            str(project["id"]),
            project["repository"],
            root,
            CodeSnapshot("a" * 40, "b" * 64, "main", CodeOrigin.OWNER),
            authorization,
            "1",
        )

    tasks = []
    for index, issue in enumerate(
        [
            "[OFFLINE FIXTURE] Approve proposed safe file edit",
            "[OFFLINE FIXTURE] Reject proposed file edit",
            "[OFFLINE FIXTURE] Edit parameters and review the new decision",
            "[OFFLINE FIXTURE] Help after repeated evidence and two adjustment prompts",
            "[OFFLINE FIXTURE] Resume after an exhausted budget",
        ]
    ):
        task_id = uuid4()
        saved = {
            "id": str(uuid4()),
            "version": "1",
            "task_id": str(task_id),
            "project_id": str(project["id"]),
            "repository": project["repository"],
            "active": True,
            "publish_draft": False,
            "issue": issue,
            "provenance": primitive(
                Provenance(Source.OWNER, "owner", "browser-offline-fixture", True)
            ),
        }
        state = (
            "paused_budget"
            if index == 4
            else "awaiting_input"
            if index == 3
            else "awaiting_approval"
        )
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO app.tasks(id,project_id,base_sha,authorization_snapshot,budget,state) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    task_id,
                    project["id"],
                    "a" * 40,
                    Jsonb(saved),
                    Jsonb({"max_calls": 3, "max_tokens": 1000}),
                    state,
                ),
            )
        ctx = context(str(task_id))
        if index < 3:
            action = Action("write_file", {"path": "src/range.py", "content": "inclusive = True\n"})
            digest = action_digest(action, ctx)
            planned = database.plan_action(task_id, "fixture-edit", digest, primitive(action))
            database.ensure_approval(task_id, planned["id"], digest, "action")
        elif index == 3:
            progress = None
            for number in range(1, 7):
                progress = observe_progress(
                    progress,
                    {"tool": "read_file", "params": {"path": "src/range.py"}},
                    {"status": "failed", "reason": "No new boundary evidence"},
                    step_id=f"fixture-{number}",
                )
                database.event(task_id, "progress.observed", progress["steps"][-1])
                if number in {2, 4}:
                    database.event(
                        task_id, "intervention.replan", {"round": number // 2, "fixture": True}
                    )
            progress["replans"] = 2
            decision = {**route_progress(progress), "calibrated": False}
            packet = help_packet(issue, progress, decision)
            database.event(task_id, "intervention.checked", decision)
            database.event(task_id, "intervention.requested", packet)
            subject = packet["subject"]
            digest = confirmation_digest(ApprovalKind.CLARIFICATION, subject, ctx)
            approval = database.ensure_approval(task_id, None, digest, "clarification")
            with database.connect() as conn:
                conn.execute(
                    "UPDATE app.approvals SET reply_payload=%s WHERE id=%s",
                    (Jsonb({"subject": subject, "question": packet["question"]}), approval["id"]),
                )
        database.event(
            task_id,
            "task.waiting",
            {
                "reason": issue,
                "required_action": "Adjust budget" if index == 4 else "Review current evidence",
            },
        )
        database.event(
            task_id,
            "repair.diff",
            {"diff": "- inclusive = False\n+ inclusive = True", "fixture": True},
        )
        tasks.append({"id": str(task_id), "issue": issue})
    candidate_id = uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.memory_candidates(id,project_id,source_task_id,fingerprint,"
            "category,content,status,applicability) VALUES(%s,%s,%s,%s,'fact',%s,"
            "'pending_review',%s)",
            (
                candidate_id,
                project["id"],
                tasks[0]["id"],
                "c" * 64,
                "[OFFLINE FIXTURE] Intervals include both endpoints",
                Jsonb({"_revision": 1}),
            ),
        )
        conn.execute(
            "INSERT INTO app.deliveries(id,task_id,branch,head_sha,tree_hash,status,pr_url) "
            "VALUES(%s,%s,%s,%s,%s,'fixture_only',%s)",
            (
                uuid4(),
                tasks[0]["id"],
                "decagent/" + tasks[0]["id"],
                "a" * 40,
                "b" * 64,
                "https://github.com/fixture/browser-workbench/pull/1",
            ),
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "database_name": name,
                "tasks": tasks,
                "memory_id": str(candidate_id),
                "project_id": str(project["id"]),
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        database_url=database.url, data_dir=root, public_url="http://127.0.0.1:8021"
    )
    return create_app(
        database,
        settings,
        callbacks=GatewayCallbacks(
            current_policy_context=context,
            memory_manager_factory=lambda project_id, scoped: MemoryManager(scoped, evidence),
        ),
    )


if __name__ == "__main__":
    uvicorn.run(build_fixture(), host="127.0.0.1", port=8021, access_log=False)
