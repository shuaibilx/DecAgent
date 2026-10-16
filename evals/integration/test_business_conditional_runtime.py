"""The conditional interpreter advances recorded edges, never a planning queue."""

import importlib

import pytest

from decagent.business.plans.bindings import BindingContext
from evals.deterministic.test_business_plan_compiler import branching_plan, read_plan
from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks
from evals.integration.test_business_plan_repository import repository

pytestmark = pytest.mark.integration


def runtime(database, sample, plan=None, *, task_input=None, hook=None):
    api = importlib.import_module("decagent.business.plans.runtime")
    task = database.get_task(sample["tasks"]["business-3"]["id"])
    if task["lease_owner"] == "plan-owner":
        from decagent.business.plans.repository import PlanRepository

        repo = PlanRepository(database, task["id"], "plan-owner", task["lease_generation"])
    else:
        repo = repository(database, sample)
    schema = {
        "type": "object",
        "properties": {"instruction": {"type": "string"}, "ticket_id": {"type": "string"}},
    }
    if repo.current() is None:
        repo.publish(plan or read_plan(), input_schema=schema)
    calls = []

    def execute(node, values, context, saved):
        calls.append(node["id"])
        data = sample["backend"].read(node["tool"]["command"], values)
        current = repo.guard()
        return api.NodeResult(
            {
                "kind": "read_tool",
                "verified": True,
                "data": data,
                "inputs": values,
                "employee_id": context.employee_id,
                "scope_revision": context.scope_revision,
                "account_revision": current["authorization_revision"],
            }
        )

    value = api.PlanRuntime(
        repo,
        execute,
        task_input=task_input
        or {"instruction": sample["tasks"]["business-3"]["authorization_snapshot"]["instruction"]},
        after_save=hook,
    )
    return value, calls, repo


def drain(value, maximum=20):
    for _ in range(maximum):
        step = value.step()
        if step["status"] != "advanced":
            return step
    raise AssertionError("Runtime failed to terminate")


def test_fixed_read_plan_completes_without_primary(database, mixed_business_tasks):
    value, calls, repo = runtime(database, mixed_business_tasks)
    result = drain(value)
    assert result["status"] == "complete" and result["completion"]["valid"]
    assert calls == ["directory"]
    assert repo.counters()["node_attempts"] == 2
    assert repo.counters()["graph_transitions"] >= 2


def test_saved_read_survives_crash_before_next_checkpoint(database, mixed_business_tasks):
    def crash(node):
        if node == "directory":
            raise SystemExit("crash after durable read")

    value, calls, repo = runtime(database, mixed_business_tasks, hook=crash)
    with pytest.raises(SystemExit):
        value.step()
    assert calls == ["directory"]
    attempts = repo.counters()["node_attempts"]
    replacement, recovered_calls, same = runtime(database, mixed_business_tasks)
    assert drain(replacement)["completion"]["valid"]
    assert recovered_calls == []
    assert same.counters()["node_attempts"] == attempts + 1


def test_inactive_branch_does_not_block_all_active_join(database, mixed_business_tasks):
    value, calls, repo = runtime(database, mixed_business_tasks, branching_plan())
    result = drain(value)
    assert result["completion"]["valid"]
    assert calls == ["directory"]
    assert {row["node_id"] for row in repo.nodes()} == {"choose", "directory", "merge", "done"}
    selected = next(row for row in repo.nodes() if row["node_id"] == "choose")
    assert selected["output_snapshot"]["control"]["targets"] == ["directory"]


def test_fanout_join_waits_for_both_active_paths(database, mixed_business_tasks):
    from evals.deterministic.test_business_plan_contracts import node

    plan = read_plan()
    plan["nodes"][0]["next"] = ["left", "right"]
    plan["nodes"].insert(1, node("left", "join", mode="all_active", next=["done"]))
    plan["nodes"].insert(2, node("right", "join", mode="all_active", next=["done"]))
    plan["nodes"][-1]["depends_on"] = ["left", "right"]
    value, calls, repo = runtime(database, mixed_business_tasks, plan)
    assert drain(value)["completion"]["valid"]
    rows = repo.nodes()
    assert next(i for i, row in enumerate(rows) if row["node_id"] == "done") == 3
    assert calls == ["directory"]


def test_persistent_control_bound_waits_and_does_not_reset(database, mixed_business_tasks):
    value, calls, repo = runtime(database, mixed_business_tasks)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET budget=budget||'{\"max_graph_transitions\":1}'::jsonb "
            "WHERE id=%s",
            (repo.task_id,),
        )
    assert value.step()["status"] == "advanced"
    replacement, _, _ = runtime(database, mixed_business_tasks)
    result = replacement.step()
    assert result["status"] == "waiting" and result["request"]["kind"] == "budget"
    assert repo.counters()["graph_transitions"] == 1
    assert calls == ["directory"]


def test_recovery_cannot_replace_task_input(database, mixed_business_tasks):
    runtime(database, mixed_business_tasks)
    with pytest.raises(ValueError, match="input"):
        runtime(database, mixed_business_tasks, task_input={"instruction": "another goal"})


def test_node_attempt_budget_is_not_reset_by_recovery(database, mixed_business_tasks):
    value, _, repo = runtime(database, mixed_business_tasks)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET budget=budget||'{\"max_node_attempts\":1}'::jsonb WHERE id=%s",
            (repo.task_id,),
        )
    assert value.step()["status"] == "advanced"
    replacement, calls, _ = runtime(database, mixed_business_tasks)
    assert replacement.step()["request"]["kind"] == "budget"
    assert repo.counters()["node_attempts"] == 1 and calls == []


def test_completion_waits_for_unjoined_active_sibling(database, mixed_business_tasks):
    from evals.deterministic.test_business_plan_contracts import node

    plan = read_plan()
    plan["nodes"][0]["next"] = ["other", "done"]
    plan["nodes"].extend(
        [
            node(
                "other",
                "read_tool",
                tool={"plugin_id": "service-tickets", "command": "list"},
                inputs={},
                next=["other_done"],
            ),
            node("other_done", "complete", depends_on=["other"], evidence=["observed"]),
        ]
    )
    value, calls, repo = runtime(database, mixed_business_tasks, plan)
    assert drain(value)["completion"]["valid"]
    assert calls == ["directory", "other"]
    rows = [row["node_id"] for row in repo.nodes()]
    assert rows.index("other") < rows.index("done")


@pytest.mark.parametrize("kind", ["branch", "transform"])
def test_execution_time_missing_value_follows_declared_exit(database, mixed_business_tasks, kind):
    from evals.deterministic.test_business_plan_contracts import literal, node, reference

    plan = read_plan()
    if kind == "branch":
        first = node(
            "first",
            "branch",
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": reference("task_input", path=["ticket_id"]),
                        "right": literal("wanted"),
                    },
                    "next": ["directory"],
                }
            ],
            on_no_match={"node_id": "directory"},
        )
    else:
        first = node(
            "first",
            "transform",
            transform={"transform_id": "normalize_title", "version": "1"},
            inputs={"title": literal("   ")},
            next=["directory"],
        )
    first["on_missing"] = {"node_id": "directory"}
    plan["entry"] = "first"
    plan["nodes"].insert(0, first)
    value, calls, repo = runtime(database, mixed_business_tasks, plan)
    assert drain(value)["completion"]["valid"]
    row = next(row for row in repo.nodes() if row["node_id"] == "first")
    assert row["status"] == "failed"
    assert row["output_snapshot"]["control"]["targets"] == ["directory"]
    assert calls == ["directory"]


def test_bounded_read_transport_recovery_preserves_original_attempts(
    database, mixed_business_tasks
):
    from types import SimpleNamespace

    from decagent.business.mcp_client import MCPTransportError
    from decagent.business.plans.handlers import BusinessNodeHandlers
    from decagent.business.plans.runtime import PlanRuntime

    _, _, repo = runtime(database, mixed_business_tasks)
    calls = []

    def read(command, params):
        calls.append(command)
        if len(calls) == 1:
            raise MCPTransportError("Injected transient read error")
        return mixed_business_tasks["backend"].read(command, params)

    handler = BusinessNodeHandlers(repo, SimpleNamespace(executor=SimpleNamespace(read=read)))
    first = PlanRuntime(repo, handler.execute)
    assert first.step()["status"] == "waiting"
    assert repo.nodes()[0]["dispatch_attempts"] == 1
    replacement = PlanRuntime(repo, handler.execute)
    assert drain(replacement)["completion"]["valid"]
    assert repo.nodes()[0]["dispatch_attempts"] == 2
    assert repo.counters()["node_attempts"] == 3


def test_final_mcp_schema_rejection_uses_declared_error_exit(database, mixed_business_tasks):
    plan = branching_plan()
    target = next(item for item in plan["nodes"] if item["id"] == "read_ticket")
    target["on_error"] = {"node_id": "directory"}
    instruction = mixed_business_tasks["tasks"]["business-3"]["authorization_snapshot"][
        "instruction"
    ]
    value, calls, repo = runtime(
        database,
        mixed_business_tasks,
        plan,
        task_input={"instruction": instruction, "ticket_id": "invented"},
    )
    assert drain(value)["completion"]["valid"]
    failed = next(row for row in repo.nodes() if row["node_id"] == "read_ticket")
    assert failed["status"] == "failed"
    assert calls == ["directory"]


def test_binding_source_expires_when_employee_authorization_changes():
    from decagent.business.plans.bindings import resolve_binding
    from evals.deterministic.test_business_plan_contracts import reference

    context = BindingContext(
        {},
        {
            "read": {
                "employee_id": "employee",
                "scope_revision": 1,
                "account_revision": 2,
                "kind": "read_tool",
                "verified": True,
                "data": {"id": "old"},
            }
        },
        {},
        {},
        "employee",
        1,
        account_revision=3,
    )
    with pytest.raises(ValueError, match="authorization|current"):
        resolve_binding(reference("node_output", node_id="read", path=["id"]), context)


@pytest.mark.parametrize("unknown", [False, True])
def test_changed_binding_on_started_node_preserves_original_wait_without_redispatch(
    database, mixed_business_tasks, unknown
):
    from decagent.business.plans.runtime import NodePending, NodeResult, PlanRuntime
    from evals.deterministic.test_business_plan_contracts import literal, node, reference

    plan = read_plan()
    plan["nodes"][0]["next"] = ["create"]
    plan["nodes"].insert(
        1,
        node(
            "create",
            "governed_write",
            command={"plugin_id": "service-tickets", "command": "create"},
            inputs={
                "title": literal("Do not repeat an interrupted write"),
                "category_id": reference(
                    "node_output", node_id="directory", path=["categories", 0, "id"]
                ),
            },
            next=["done"],
        ),
    )
    repo = repository(database, mixed_business_tasks)
    repo.publish(plan)
    calls = []
    original = {
        "kind": "action" if not unknown else "reconciliation",
        "operation_id": "original-operation",
        **({"approval_id": "original-approval"} if not unknown else {}),
    }

    def execute(node, values, context, saved):
        calls.append(node["id"])
        if node["id"] == "create":
            raise NodePending(original, unknown=unknown)
        return NodeResult(
            {
                "kind": "read_tool",
                "verified": True,
                "data": mixed_business_tasks["backend"].read("directory", {}),
                "employee_id": context.employee_id,
                "scope_revision": context.scope_revision,
                "account_revision": context.account_revision,
            }
        )

    initial = PlanRuntime(repo, execute)
    assert initial.step()["status"] == "advanced"
    assert initial.step()["request"] == original
    saved = repo.nodes()[-1]
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (repo.guard(conn)["business_scope_id"],),
        )
    recovered = PlanRuntime(repo, execute).step()
    assert recovered["status"] == "waiting"
    assert recovered["request"]["category"] == "binding_source_changed"
    assert recovered["request"]["operation_id"] == "original-operation"
    assert "approval_id" not in recovered["request"]
    if not unknown:
        assert recovered["request"]["original_approval_id"] == "original-approval"
    assert calls == ["directory", "create"]
    assert repo.nodes()[-1] == saved
