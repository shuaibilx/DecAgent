"""Hydrate policy only from authenticated application records and broker snapshots."""

import hashlib

from decagent.policy import (
    CodeOrigin,
    CodeSnapshot,
    CodeTrust,
    ExecutionBoundary,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
    TrustKind,
)
from decagent.policy.canonical import digest


def provenance(saved):
    return Provenance(
        Source(saved["source"]), saved["owner_id"], saved["session_id"], saved["authenticated"]
    )


def current_context_for(database, settings, task_id, *, snapshot=None, boundary=None):
    task = database.get_task(task_id)
    project = database.get_project(task["project_id"])
    saved = task["authorization_snapshot"]
    auth = OwnerAuthorization(
        saved["id"],
        str(saved["version"]),
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        provenance(saved["provenance"]),
        saved.get("publish_draft", False),
        saved.get("active", True),
    )
    if snapshot is None:
        with database.connect() as conn:
            event = conn.execute(
                "SELECT payload FROM app.events WHERE task_id=%s "
                "AND kind='runtime.snapshot' ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        snapshot = (
            event["payload"]
            if event
            else {
                "tree_digest": hashlib.sha256(
                    ("uninitialized:" + task["base_sha"]).encode()
                ).hexdigest(),
                "boundary_verified": False,
            }
        )
    trust = None
    ownership = project.get("trust") or {}
    is_owner = (
        ownership.get("source") == "github_authenticated_diagnostic"
        and ownership.get("ownership_verified") is True
        and ownership.get("code_origin") == "owner"
        and type(ownership.get("repository_id")) is int
        and ownership["repository_id"] > 0
        and str(ownership.get("authenticated_owner_login", "")).casefold()
        == project["repository"].split("/")[0].casefold()
        and str(ownership.get("repository_owner_login", "")).casefold()
        == project["repository"].split("/")[0].casefold()
    )
    stored = saved.get("code_trust")
    if stored:
        trust = CodeTrust(
            stored["id"],
            stored["version"],
            stored["repository"],
            TrustKind(stored["kind"]),
            stored["sha"],
            provenance(stored["provenance"]),
            stored.get("branch"),
            stored.get("active", True),
        )
    elif saved.get("trust_scope"):
        # This scope is an authenticated owner submission, never inferred from repository text.
        scope = saved["trust_scope"]
        if scope["kind"] == "exact_commit":
            trust = CodeTrust(
                saved["id"] + ":trust",
                str(saved["version"]),
                project["repository"],
                TrustKind.EXACT_COMMIT,
                task["base_sha"],
                auth.provenance,
            )
        elif (
            scope["kind"] == "own_branch"
            and is_owner
            and scope.get("branch") == project["base_branch"]
        ):
            trust = CodeTrust(
                saved["id"] + ":trust",
                str(saved["version"]),
                project["repository"],
                TrustKind.OWN_BRANCH,
                task["base_sha"],
                auth.provenance,
                scope["branch"],
            )
    root = settings.data_dir / "policy" / str(task["id"])
    root.mkdir(parents=True, exist_ok=True)
    verified = snapshot.get("boundary_verified") is True
    boundary = boundary or ExecutionBoundary(verified, verified, verified, verified)
    return PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        root,
        CodeSnapshot(
            task["base_sha"],
            snapshot["tree_digest"],
            project["base_branch"],
            CodeOrigin.OWNER if is_owner else CodeOrigin.EXTERNAL,
        ),
        auth,
        "1",
        trust,
        boundary,
        task["base_sha"],
        project["base_branch"],
    )


def authorization_version(context):
    # Include identity, active status, publication permission and owner provenance.
    return digest("decagent.semantic-authorization.v1", context.authorization)
