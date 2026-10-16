from dataclasses import replace
from uuid import uuid4

import pytest

from decagent.policy import (
    Action,
    ApprovalReply,
    CodeOrigin,
    CodeSnapshot,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    ReplyDecision,
    Source,
    action_digest,
)
from decagent.storage.database import Database

pytestmark = pytest.mark.integration


def test_durable_reply_edit_is_regoverned_and_old_approval_cannot_execute(database, tmp_path):
    from decagent.storage.approval_service import DurableApprovals

    db = database
    project = db.add_project("fixture/durable-approval")
    task = db.create_task(project["id"], "a" * 40, {})
    provenance = Provenance(Source.OWNER, "owner", "session", True)
    auth = OwnerAuthorization(
        "auth", "1", str(task["id"]), str(project["id"]), project["repository"], provenance
    )
    context = PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 40, "main", CodeOrigin.OWNER),
        auth,
        "1",
    )
    action = Action("write_file", {"path": "hello.py", "content": "hello"})
    digest = action_digest(action, context)
    record = db.plan_action(task["id"], "write:1", digest, {})
    approval = db.ensure_approval(task["id"], record["id"], digest, "action")
    service = DurableApprovals(db)
    reply = ApprovalReply(
        str(uuid4()),
        digest,
        ReplyDecision.EDIT,
        provenance,
        edited_params={"path": "../escape", "content": "bad"},
    )
    result = service.reply(approval["id"], reply, action=action, context=context)
    assert result.decision.hard_denial
    assert service.reply(approval["id"], reply, action=action, context=context).replayed
    with pytest.raises(ValueError):
        service.claim(approval["id"], digest, "execution:1")


def test_durable_exact_reply_claim_is_once_and_rejects_untrusted_owner(database, tmp_path):
    from decagent.storage.approval_service import DurableApprovals

    db = database
    project = db.add_project("fixture/durable-approval-claim")
    task = db.create_task(project["id"], "a" * 40, {})
    provenance = Provenance(Source.OWNER, "owner", "session", True)
    auth = OwnerAuthorization(
        "auth", "1", str(task["id"]), str(project["id"]), project["repository"], provenance
    )
    context = PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 40, "main", CodeOrigin.OWNER),
        auth,
        "1",
    )
    action = Action("write_file", {"path": "hello.py", "content": "hello"})
    digest = action_digest(action, context)
    record = db.plan_action(task["id"], "write:1", digest, {})
    approval = db.ensure_approval(task["id"], record["id"], digest, "action")
    service = DurableApprovals(db)
    fake = ApprovalReply(
        str(uuid4()),
        digest,
        ReplyDecision.APPROVE,
        Provenance(Source.REPOSITORY, "owner", "session", True),
    )
    with pytest.raises(ValueError, match="owner"):
        service.reply(approval["id"], fake, action=action, context=context)
    reply = replace(fake, provenance=provenance)
    service.reply(approval["id"], reply, action=action, context=context)
    with pytest.raises(ValueError, match="stale"):
        service.claim(
            approval["id"],
            action_digest(action, replace(context, policy_version="2")),
            "execution:1",
        )
    assert not service.claim(approval["id"], digest, "execution:1").replayed
    assert service.claim(approval["id"], digest, "execution:1").replayed
    with pytest.raises(ValueError, match="different execution"):
        service.claim(approval["id"], digest, "execution:2")


def test_long_edited_action_replays_exactly_after_restart(database, tmp_path):
    from decagent.storage.approval_service import DurableApprovals

    db = database
    project = db.add_project("fixture/long-edit")
    task = db.create_task(project["id"], "a" * 40, {})
    provenance = Provenance(Source.OWNER, "owner", "session", True)
    auth = OwnerAuthorization(
        "auth", "1", str(task["id"]), str(project["id"]), project["repository"], provenance
    )
    context = PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 40, "main", CodeOrigin.OWNER),
        auth,
        "1",
    )
    action = Action("write_file", {"path": "hello.py", "content": "initial"})
    digest = action_digest(action, context)
    record = db.plan_action(
        task["id"],
        "long:1",
        digest,
        {"tool": "write_file", "params": {"path": "hello.py", "content": "x" * 100000}},
    )
    assert len(record["snapshot"]["params"]["content"]) == 100000
    approval = db.ensure_approval(task["id"], record["id"], digest, "action")
    reply = ApprovalReply(
        str(uuid4()),
        digest,
        ReplyDecision.EDIT,
        provenance,
        edited_params={"path": "hello.py", "content": "x" * 100000},
    )
    result = DurableApprovals(db).reply(approval["id"], reply, action=action, context=context)
    replay = DurableApprovals(Database(db.url)).reply(
        approval["id"], reply, action=action, context=context
    )
    assert result.edited_action.params == replay.edited_action.params
    assert action_digest(replay.edited_action, context) == replay.decision.digest
