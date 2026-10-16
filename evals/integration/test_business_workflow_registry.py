"""Isolated database regressions; scripted reports make no model quality claims."""

import importlib
from copy import deepcopy
from importlib.resources import files

import psycopg
import pytest

from decagent.business.access import AccessError
from decagent.business.plans.bindings import resolve_inputs
from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import digest
from decagent.gateway.auth import OwnerIdentity
from evals.deterministic.test_business_plan_compiler import context
from evals.deterministic.test_business_plan_contracts import node, reference
from evals.deterministic.test_business_workflow_contracts import definition, report
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


@pytest.fixture
def sample(database):
    backend = FixtureTickets(database, login="supervisor")
    scope = PluginRegistry(database).list()[0]
    with database.connect() as conn:
        if not conn.execute("SELECT to_regclass('app.business_workflows') AS t").fetchone()["t"]:
            conn.execute(
                files("decagent.storage").joinpath("017_business_workflows.sql").read_text()
            )
        conn.execute("INSERT INTO app.owner_credentials VALUES(true,%s)", ("f" * 64,))
    module = importlib.import_module("decagent.business.workflow_registry")
    value = definition(str(scope["id"]))
    value["scope_revision"] = scope["revision"]
    return {
        "store": module.WorkflowRegistry(database),
        "backend": backend,
        "owner": OwnerIdentity("bearer:" + "f" * 64, True),
        "definition": value,
        "scope": scope,
        "database": database,
    }


def publish(sample, value=None):
    value = value or sample["definition"]
    store, owner = sample["store"], sample["owner"]
    store.register(owner, value)
    evidence = report(value)
    evidence["report_id"] = "report-" + value["workflow_id"] + "-" + value["version"]
    store.validate(owner, value["workflow_id"], value["version"], evidence, digest(evidence))
    return store.publish(owner, value["workflow_id"], value["version"])


def match(sample, **kwargs):
    return sample["store"].match(
        sample["backend"].identity,
        str(sample["scope"]["id"]),
        kwargs.pop("inputs", {"instruction": "Read today's directory"}),
        **kwargs,
    )


def task(sample, backend=None, instruction="Read directory"):
    backend = backend or sample["backend"]
    return sample["database"].create_business_task(
        sample["scope"]["id"],
        {"plugin_digest": sample["scope"]["manifest_digest"], "instruction": instruction},
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )


def test_registry_is_empty_and_draft_success_cannot_implicitly_publish(sample):
    assert match(sample, workflow_id="read-directory").reason == "workflow_unavailable"
    row = sample["store"].register(sample["owner"], sample["definition"])
    assert row["status"] == "draft"
    assert match(sample, workflow_id="read-directory").reason == "workflow_unavailable"
    with pytest.raises(ValueError, match="validated"):
        sample["store"].publish(sample["owner"], "read-directory", "1")


def test_owner_publishes_only_validated_immutable_versions_and_report(sample):
    published = publish(sample)
    assert published["status"] == "published"
    assert match(sample, workflow_id="read-directory", version="1").matched
    changed = deepcopy(sample["definition"])
    changed["goal_id"] = "a-different-goal"
    with pytest.raises(ValueError, match="immutable"):
        sample["store"].register(sample["owner"], changed)
    with sample["database"].connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE app.business_workflows SET definition='{}'")
    with sample["database"].connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM app.workflow_validation_reports")


def test_employee_and_revoked_owner_cannot_manage_workflows(sample):
    store = sample["store"]
    with pytest.raises(AccessError):
        store.register(sample["backend"].identity, sample["definition"])
    publish(sample)
    with sample["database"].connect() as conn:
        conn.execute("UPDATE app.owner_credentials SET token_hash=%s", ("e" * 64,))
    with pytest.raises(AccessError):
        store.revoke(sample["owner"], "read-directory", "1", "withdrawn")


def test_structured_matching_requires_explicit_target_and_required_inputs(sample):
    publish(sample)
    assert match(sample).reason == "clarification_required"
    missing = match(sample, workflow_id="read-directory", inputs={})
    assert missing.reason == "missing_input"
    assert missing.missing_fields == ("instruction",)
    assert (
        match(sample, workflow_id="read-directory", inputs={"instruction": 42}).reason
        == "invalid_input"
    )
    assert match(sample, goal_id="directory-observation").matched
    other = deepcopy(sample["definition"])
    other["workflow_id"] = "second-directory"
    publish(sample, other)
    result = match(sample, goal_id="directory-observation")
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


def test_applicability_and_multiple_versions_never_silently_choose(sample):
    value = deepcopy(sample["definition"])
    value["applicability"]["roles"] = ["handler"]
    publish(sample, value)
    assert match(sample, workflow_id="read-directory").reason == "not_applicable"
    value["version"] = "2"
    value["applicability"]["roles"] = ["supervisor"]
    publish(sample, value)
    assert match(sample, workflow_id="read-directory").matched
    value["version"] = "3"
    publish(sample, value)
    assert match(sample, workflow_id="read-directory").reason == "ambiguous"
    assert match(sample, workflow_id="read-directory", version="2").matched


def test_instances_rebind_inputs_employee_and_current_directory_without_old_outputs(sample):
    publish(sample)
    store = sample["store"]
    first_task = task(sample, instruction="Current directory for first employee")
    first = store.instantiate(
        sample["backend"].identity,
        first_task["id"],
        "read-directory",
        "1",
        {"instruction": "Current directory for first employee"},
    )
    other = FixtureTickets(sample["database"], login="employee-it")
    second_task = task(sample, other, instruction="Current directory for second employee")
    second = store.instantiate(
        other.identity,
        second_task["id"],
        "read-directory",
        "1",
        {"instruction": "Current directory for second employee"},
    )
    assert first.plan.plan_id != second.plan.plan_id
    assert first.binding_context.employee_id != second.binding_context.employee_id
    assert first.binding_context.task_input != second.binding_context.task_input
    assert first.binding_context.node_outputs == second.binding_context.node_outputs == {}
    assert (
        first.binding_context.accepted_decisions == second.binding_context.accepted_decisions == {}
    )
    assert (
        resolve_inputs(first.plan.nodes[0], first.binding_context, sample["backend"].store.spec)[0]
        == {}
    )
    before = sample["backend"].read("directory", {})
    with sample["database"].connect() as conn:
        conn.execute(
            "UPDATE business.people SET name=name || ' current' WHERE name=%s",
            (before["people"][0]["name"],),
        )
    after = sample["backend"].read("directory", {})
    assert before != after
    assert first.binding_context.node_outputs == {}
    with pytest.raises(AccessError):
        store.instance(other.identity, first_task["id"])
    again = store.instantiate(
        sample["backend"].identity,
        first_task["id"],
        "read-directory",
        "1",
        {"instruction": "Current directory for first employee"},
    )
    assert again.plan.digest == first.plan.digest


def test_revocation_and_scope_drift_stop_new_hits_but_retain_instance_history(sample):
    publish(sample)
    store, identity = sample["store"], sample["backend"].identity
    current_task = task(sample, instruction="Current directory")
    instance = store.instantiate(
        identity, current_task["id"], "read-directory", "1", {"instruction": "Current directory"}
    )
    store.revoke(sample["owner"], "read-directory", "1", "regression withdrawn")
    assert match(sample, workflow_id="read-directory", version="1").reason == "workflow_revoked"
    assert store.check_instance(identity, current_task["id"]).reason == "workflow_revoked"
    assert store.instance(identity, current_task["id"]).plan.digest == instance.plan.digest
    with pytest.raises(ValueError):
        store.publish(sample["owner"], "read-directory", "1")
    with sample["database"].connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.workflow_instances").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 0


def test_scope_revision_change_invalidates_template_without_changing_history(sample):
    publish(sample)
    with sample["database"].connect() as conn:
        conn.execute("UPDATE app.business_scopes SET revision=revision+1")
    assert match(sample, workflow_id="read-directory").reason == "scope_changed"


def test_synthetic_workflow_regression_cannot_supply_jev_quality_or_authority(sample):
    value = deepcopy(sample["definition"])
    question = context()[1].get("person")
    decide = node(
        "select-person",
        "decide",
        depends_on=["directory"],
        question={"question_id": "person", "version": question["version"], "type": "choice"},
        policy={"policy_id": "assignment", "version": "1"},
        inputs={
            "instruction": reference("task_input", path=["instruction"]),
            "directory": reference("node_output", node_id="directory", value_type="object"),
        },
        next=["done"],
        on_no_match={"stop": "missing_input"},
    )
    value["plan"]["nodes"][0]["next"] = ["select-person"]
    value["plan"]["nodes"].insert(1, decide)
    value["plan"]["nodes"][-1]["depends_on"] = ["directory", "select-person"]
    value["policy_dependencies"] = [
        {"policy_id": "assignment", "policy_version": "1", "definition_digest": "a" * 64}
    ]
    sample["store"].register(sample["owner"], value)
    evidence = report(value)
    evidence["covered_nodes"] = ["directory", "select-person", "done"]
    evidence["covered_edges"] = [["directory", "select-person"], ["select-person", "done"]]
    with pytest.raises(ValueError, match="policy_changed"):
        sample["store"].validate(sample["owner"], "read-directory", "1", evidence, digest(evidence))
    with pytest.raises(ValueError, match="validated"):
        sample["store"].publish(sample["owner"], "read-directory", "1")


def test_revoked_employee_and_changed_roles_cannot_reuse_authority(sample):
    value = deepcopy(sample["definition"])
    value["applicability"]["roles"] = ["supervisor"]
    publish(sample, value)
    assert match(sample, workflow_id="read-directory").matched
    with sample["database"].connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET role='employee' WHERE id=%s",
            (sample["backend"].identity.employee_id,),
        )
    assert match(sample, workflow_id="read-directory").reason == "not_applicable"
    with sample["database"].connect() as conn:
        conn.execute(
            "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s",
            (sample["backend"].identity.credential_ref,),
        )
    with pytest.raises(AccessError):
        match(sample, workflow_id="read-directory")


def test_instance_cannot_replace_task_instruction(sample):
    publish(sample)
    current = task(sample)
    with pytest.raises(AccessError, match="instruction"):
        sample["store"].instantiate(
            sample["backend"].identity,
            current["id"],
            "read-directory",
            "1",
            {"instruction": "Another goal"},
        )


def test_new_account_revision_cannot_reuse_old_instance_binding_context(sample):
    publish(sample)
    current = task(sample)
    store = sample["store"]
    first = store.instantiate(
        sample["backend"].identity,
        current["id"],
        "read-directory",
        "1",
        {"instruction": "Read directory"},
    )
    with sample["database"].connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE id=%s",
            (sample["backend"].identity.employee_id,),
        )
    renewed = FixtureTickets(sample["database"], login="supervisor")
    assert store.instance(renewed.identity, current["id"]).plan.digest == first.plan.digest
    assert store.check_instance(renewed.identity, current["id"]).reason == "permission_changed"
    with pytest.raises(AccessError, match="revision"):
        store.instantiate(
            renewed.identity,
            current["id"],
            "read-directory",
            "1",
            {"instruction": "Read directory"},
        )


@pytest.mark.parametrize(
    "field,reason", [("scope_revision", "scope_changed"), ("report_digest", "report_changed")]
)
def test_instance_snapshot_fingerprints_are_checked_even_after_database_corruption(
    sample, field, reason
):
    publish(sample)
    current = task(sample)
    store = sample["store"]
    store.instantiate(
        sample["backend"].identity,
        current["id"],
        "read-directory",
        "1",
        {"instruction": "Read directory"},
    )
    # This isolated case deliberately bypasses the immutable-row trigger to test
    # read-time integrity checks; normal app roles cannot perform this mutation.
    with sample["database"].connect() as conn:
        conn.execute("ALTER TABLE app.workflow_instances DISABLE TRIGGER USER")
        if field == "scope_revision":
            conn.execute("UPDATE app.workflow_instances SET scope_revision=scope_revision+1")
        else:
            conn.execute("UPDATE app.workflow_instances SET report_digest=%s", ("a" * 64,))
        conn.execute("ALTER TABLE app.workflow_instances ENABLE TRIGGER USER")
    assert store.check_instance(sample["backend"].identity, current["id"]).reason == reason
