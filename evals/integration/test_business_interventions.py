"""Persistent semantic waits are current, actor-bound and separate from write approval."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from decagent.business.access import AccessError
from decagent.business.decision_policy import PolicyContext, gate_decision
from decagent.business.interventions import SemanticInterventions, decision_evidence
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.registry import PluginRegistry
from decagent.gateway.models import SemanticReplyInput
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import node, reference
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(database):
    def create(kind="choice"):
        backend = FixtureTickets(database, login="employee-it")
        scope = PluginRegistry(database).list()[0]
        task = database.create_business_task(
            scope["id"],
            {
                "instruction": "Find the requested employee",
                "plugin_digest": scope["manifest_digest"],
            },
            graph_version="business-3",
            employee_id=backend.identity.employee_id,
            credential_ref=backend.identity.credential_ref,
        )
        held = database.claim(task["id"], "semantic-worker", seconds=120)
        repo = PlanRepository(database, task["id"], "semantic-worker", held["lease_generation"])
        registry = QuestionRegistry.from_spec(repo.spec)
        qid = {"choice": "person", "noul": "explicit_assignee", "score": "work_blockage"}[kind]
        definition = registry.get(qid)
        judge = node(
            "judge",
            "decide",
            question={"question_id": qid, "version": definition["version"], "type": kind},
            policy={"policy_id": "unavailable", "version": "1"},
            inputs={
                "instruction": reference("task_input", path=["instruction"]),
                "directory": reference("node_output", node_id="directory", value_type="object"),
            },
            next=["done"],
            on_no_match={"stop": "no_match"},
        )
        plan = read_plan()
        plan["nodes"][0]["next"] = ["judge"]
        plan["nodes"].insert(1, judge)
        plan["nodes"][-1]["depends_on"] = ["judge"]
        saved = repo.publish(plan)
        directory = backend.read("directory", {})
        repo.begin("directory", {}, {})
        repo.finish(
            "directory",
            "succeeded",
            {"result": {"verified": True, "data": directory}, "control": {"targets": ["judge"]}},
        )
        state = {
            "instruction": task["authorization_snapshot"]["instruction"],
            "directory": directory,
            "ticket": None,
            "sources": {},
        }
        repo.begin("judge", state, {})
        context = PolicyContext(
            repo.spec.digest,
            repo.spec.manifest["workflow"]["version"],
            "uncalibrated",
            "semantic",
            definition["candidate_source"] or "workflow." + qid,
        )

        def refresh(conn, current_task, pending, identity):
            state = deepcopy(pending["binding"]["evidence"])
            state["directory"] = backend.read("directory", {})
            return decision_evidence(
                repo.spec,
                current_task,
                pending["binding"]["node"],
                state,
                context=context,
                scope_version=current_task["scope_revision"],
                account_version=identity.account_revision,
            )

        service = SemanticInterventions(database, refresh=refresh)
        current = repo.guard()
        evidence = decision_evidence(
            repo.spec,
            current,
            judge,
            state,
            context=context,
            scope_version=current["scope_revision"],
            account_version=backend.identity.account_revision,
        )
        if kind == "choice":
            criteria = evidence.question.criteria
            answer = {
                "type": "choice",
                "choice": next(iter(criteria)),
                "probabilities": {key: 1 / len(criteria) for key in criteria},
                "confidence": 0,
            }
        elif kind == "noul":
            answer = {"type": "noul", "noul": 0.5}
        else:
            n = len(evidence.question.criteria)
            answer = {
                "type": "score",
                "score": (n - 1) / 2,
                "probabilities": {str(i): 1 / n for i in range(n)},
                "confidence": 0,
                "legend": {str(i): value for i, value in enumerate(evidence.question.criteria)},
            }
        gate = gate_decision(
            evidence.question,
            answer,
            actual_model="jev-1.2.3",
            snapshot=evidence.snapshot,
            current_snapshot=evidence.snapshot,
            policy=None,
            now=datetime.now(UTC),
        )
        result = {
            "backend": backend,
            "repo": repo,
            "service": service,
            "task": task,
            "evidence": evidence,
            "gate": gate,
            "plan": saved,
            "refresh": refresh,
        }
        return result

    return create


def request(sample, **changes):
    repo = sample["repo"]
    options = {
        "identity": sample["backend"].identity,
        "lease_owner": repo.owner,
        "lease_generation": repo.generation,
    }
    options.update(changes)
    return sample["service"].request(
        repo.task_id,
        "judge",
        sample["plan"].revision,
        sample["evidence"],
        sample["gate"],
        **options,
    )


def waiting(database, sample):
    pending = request(sample)
    sample["repo"].finish("judge", "waiting", {"request": pending})
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (sample["task"]["id"],),
        )
    return pending


def body(pending, decision="select", value=None, text=None):
    return SemanticReplyInput(
        expected_digest=pending["digest"],
        expected_plan_revision=pending["plan_revision"],
        decision=decision,
        value=value,
        text=text,
    )


def submit(sample, pending, value=None, decision="select", text=None, identity=None):
    return sample["service"].reply(
        identity or sample["backend"].browser,
        pending["approval_id"],
        body(pending, decision, value, text),
        response_id="test-response",
    )


def consume(database, sample, pending, consumer_id="judge:1"):
    task = database.get_task(sample["task"]["id"])
    if task["state"] != "running":
        task = database.claim(sample["task"]["id"], "semantic-resume", seconds=120)
    return sample["service"].consume(
        pending["approval_id"],
        identity=sample["backend"].identity,
        lease_owner=task["lease_owner"],
        lease_generation=task["lease_generation"],
        consumer_id=consumer_id,
    )


@pytest.mark.parametrize(
    "kind,value", [("choice", None), ("noul", True), ("noul", False), ("score", 2)]
)
def test_authenticated_primitive_selection_survives_restart_and_is_consumed_once(
    database, setup, kind, value
):
    sample = setup(kind)
    pending = waiting(database, sample)
    if value is None:
        value = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    receipt = submit(sample, pending, value)
    assert receipt["status"] == "replied"
    assert submit(sample, pending, value)["replayed"]
    sample["service"] = SemanticInterventions(database, refresh=sample["refresh"])
    result = consume(database, sample, pending)
    assert result["status"] == "selected" and result["selected_value"] == value
    assert not result["grants_write_authority"]
    assert result["raw_model_answer"] == sample["gate"].answer.to_dict()
    assert consume(database, sample, pending)["replayed"]
    with pytest.raises(AccessError):
        consume(database, sample, pending, consumer_id="another-consumer")
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM business.operations WHERE source='decagent'"
            ).fetchone()["n"]
            == 0
        )
        row = conn.execute(
            "SELECT reply_payload FROM app.approvals WHERE id=%s", (pending["approval_id"],)
        ).fetchone()
        assert "policy_record" not in row["reply_payload"]
        assert "semantic_record" in row["reply_payload"]


@pytest.mark.parametrize(
    "decision,text,status",
    [
        ("no_match", None, "no_match"),
        ("supplement", "Need the Facilities team", "supplement"),
        ("reject", "Stop here", "rejected"),
    ],
)
def test_nonselection_replies_remain_explicit_and_never_force_the_model_choice(
    database, setup, decision, text, status
):
    sample = setup()
    pending = waiting(database, sample)
    submit(sample, pending, decision=decision, text=text)
    result = consume(database, sample, pending)
    assert result["status"] == status
    assert result["selected_value"] is None
    assert result["text"] == text


def test_no_response_remains_waiting_after_restart_without_duplicate_pending(database, setup):
    sample = setup()
    first, repeated = request(sample), request(sample)
    assert first["approval_id"] == repeated["approval_id"]
    sample["service"] = SemanticInterventions(database, refresh=sample["refresh"])
    result = consume(database, sample, first)
    assert result["status"] == "waiting"
    assert not result["primary_fallback"]


def test_inactive_branch_never_creates_a_wait_or_consumes_a_low_score(database, setup):
    sample = setup()
    result = request(sample, active=False)
    assert result["status"] == "inactive"
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.business_decision_interventions"
            ).fetchone()["n"]
            == 0
        )


def test_foreign_employee_mcp_reply_and_conflicting_retry_are_denied(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    other = FixtureTickets(database, login="employee-facilities")
    for identity in (other.browser, sample["backend"].identity):
        with pytest.raises(AccessError):
            submit(sample, pending, selected, identity=identity)
    submit(sample, pending, selected)
    with pytest.raises(AccessError):
        submit(sample, pending, decision="reject", text="Conflicting retry")


def test_candidate_withdrawal_refreshes_pending_and_does_not_consume_old_selection(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    result = submit(sample, pending, selected)
    assert result["status"] == "stale"
    assert result["refresh_required"]
    assert selected not in result["request"]["question"]["criteria"]
    assert database.get_task(sample["task"]["id"])["state"] == "queued"
    with database.connect() as conn:
        old = conn.execute(
            "SELECT reply_payload,status FROM app.approvals WHERE id=%s", (pending["approval_id"],)
        ).fetchone()
        control = old["reply_payload"]["semantic_record"]
    assert old["status"] == "consumed"
    assert control["kind"] == "semantic_refresh" and control["status"] == "stale"
    assert control["request"]["approval_id"] == result["request"]["approval_id"]
    assert control["selected_value"] is None and not control["may_resume"]


def test_expired_reply_creates_fresh_wait_without_silent_consent(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    sample["service"].clock = lambda: datetime.now(UTC) + timedelta(days=2)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    result = submit(sample, pending, selected)
    assert result["status"] == "expired"
    assert result["request"]["approval_id"] != pending["approval_id"]
    assert result["request"]["suggestion"] is None


def test_reply_is_revalidated_at_consume_after_candidate_or_scope_change(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    submit(sample, pending, selected)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (sample["task"]["business_scope_id"],),
        )
    result = consume(database, sample, pending)
    assert result["status"] == "stale" and not result["may_resume"]


def test_credential_revision_change_and_old_plan_revision_cannot_advance(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    with pytest.raises(AccessError):
        sample["service"].reply(
            sample["backend"].browser,
            pending["approval_id"],
            replace_body := body(pending, value=selected).model_copy(
                update={"expected_plan_revision": 2}
            ),
            response_id="old-plan",
        )
    assert replace_body.expected_plan_revision == 2
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE id=%s",
            (sample["backend"].identity.employee_id,),
        )
    with pytest.raises(AccessError):
        submit(sample, pending, selected)


def test_unanswered_consume_refreshes_current_candidates_before_display(database, setup):
    sample = setup()
    pending = request(sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    result = consume(database, sample, pending)
    assert result["status"] == "stale" and not result["may_resume"]
    assert selected not in result["request"]["question"]["criteria"]


def test_terminal_stale_wait_cannot_be_answered_after_candidates_are_restored(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    assert submit(sample, pending, selected)["status"] == "stale"
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=true WHERE id=%s", (selected,))
    result = submit(sample, pending, selected)
    assert result["status"] == "stale" and not result["may_resume"]
    with database.connect() as conn:
        old = conn.execute(
            "SELECT * FROM app.business_decision_interventions WHERE approval_id=%s",
            (pending["approval_id"],),
        ).fetchone()
        assert old["status"] == "stale" and old["response_record"] is None
    assert database.get_task(sample["task"]["id"])["state"] == "queued"


@pytest.mark.parametrize(
    "kind,value", [("choice", "invented-id"), ("noul", 1), ("score", True), ("score", 99)]
)
def test_illegal_primitive_selection_cannot_queue_task(database, setup, kind, value):
    sample = setup(kind)
    pending = waiting(database, sample)
    with pytest.raises(AccessError, match="legal choices"):
        submit(sample, pending, value)
    assert database.get_task(sample["task"]["id"])["state"] == "awaiting_input"


def test_missing_refresh_in_restarted_service_never_resumes_unanswered_wait(database, setup):
    sample = setup()
    pending = request(sample)
    sample["service"] = SemanticInterventions(database)
    with pytest.raises(AccessError) as error:
        consume(database, sample, pending)
    assert error.value.code == "service_unavailable"


def test_completed_decision_cannot_create_a_new_semantic_wait(database, setup):
    sample = setup()
    sample["repo"].finish("judge", "succeeded", {"control": {"targets": ["done"]}})
    with pytest.raises(AccessError) as error:
        request(sample)
    assert error.value.code == "inactive_decision"


def test_api_refresh_can_be_consumed_as_control_without_reusing_old_selection(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    refreshed = submit(sample, pending, selected)
    result = consume(database, sample, pending)
    assert result["semantic_record"]["kind"] == "semantic_refresh"
    assert result["request"]["approval_id"] == refreshed["request"]["approval_id"]
    assert result["semantic_record"]["selected_value"] is None
    assert not result["may_resume"]
    assert database.get_task(sample["task"]["id"])["state"] == "running"


def test_old_finished_semantic_reply_cannot_interrupt_a_later_wait(database, setup):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    submit(sample, pending, selected)
    consume(database, sample, pending)
    held = database.get_task(sample["task"]["id"])
    repo = PlanRepository(database, held["id"], held["lease_owner"], held["lease_generation"])
    repo.finish("judge", "succeeded", {"control": {"targets": ["done"]}})
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_approval',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (held["id"],),
        )
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    result = submit(sample, pending, selected)
    assert result["status"] == "stale" and result["request"] is None
    assert database.get_task(held["id"])["state"] == "awaiting_approval"
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.business_decision_interventions"
            ).fetchone()["n"]
            == 1
        )
