"""Goal evidence survives omitted auxiliary reads without hiding writes or invented IDs."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.planner import PlannerProtocol
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest


def observation(command, *, ticket=None, params=None):
    return {
        "action_id": str(uuid4()),
        "command": command,
        "params": params or {},
        "data": {"ticket": {"id": ticket}} if ticket else {},
    }


class DurableEvidence:
    def __init__(self, observations):
        self.connections = 0
        self.rows = [
            {
                "id": item["action_id"],
                "status": "succeeded",
                "receipt": {"ok": True, "data": item["data"]},
                "snapshot": {"command": item["command"], "params": item["params"]},
            }
            for item in observations
        ]

    @contextmanager
    def connect(self):
        self.connections += 1
        yield self

    def execute(self, query, params):
        return SimpleNamespace(fetchall=lambda: self.rows)


@pytest.fixture
def service():
    instance = BusinessServices.__new__(BusinessServices)
    instance.spec = PluginSpec.from_mapping(builtin_manifest())
    instance.protocol = PlannerProtocol(instance.spec)
    instance.task = {"id": str(uuid4())}
    return instance


def finish(service, observations, claimed, instruction):
    service.database = DurableEvidence(observations)
    data = {
        "instruction": instruction,
        "observations": observations,
        "queue": [
            {
                "command": "decagent_finish",
                "params": {"summary": "Model says done", "completed_actions": claimed},
            }
        ],
    }
    validation = service._validate(data)
    return validation, service._finalize({**data, **validation})


@pytest.mark.parametrize("auxiliary", ["directory", "list", "get", "receipt"])
def test_completed_create_assign_does_not_require_citing_auxiliary_reads(service, auxiliary):
    ticket = str(uuid4())
    created = observation("create", ticket=ticket)
    assigned = observation("assign", params={"ticket_id": ticket})
    readings = [observation(auxiliary)]
    observations = [*readings, created, assigned]
    validation, result = finish(
        service,
        observations,
        [created["action_id"], assigned["action_id"]],
        "创建工单；分配工单给 Chen Li",
    )
    assert validation["completion"]["valid"] is True
    assert result["completion"]["valid"] is True
    assert result["completion"]["actions"] == [item["action_id"] for item in observations]


def test_all_action_ids_remain_an_accepted_completion_claim(service):
    ticket = str(uuid4())
    observations = [
        observation("directory"),
        observation("create", ticket=ticket),
        observation("assign", params={"ticket_id": ticket}),
    ]
    _, result = finish(
        service,
        observations,
        [item["action_id"] for item in observations],
        "创建工单；分配工单给 Chen Li",
    )
    assert result["completion"]["valid"] is True


def test_omitting_any_executed_write_is_rejected(service):
    ticket = str(uuid4())
    created = observation("create", ticket=ticket)
    assigned = observation("assign", params={"ticket_id": ticket})
    validation, result = finish(
        service, [created, assigned], [created["action_id"]], "创建工单；分配工单给 Chen Li"
    )
    assert validation["completion"]["valid"] is False
    assert result["completion"]["valid"] is False


def test_unknown_or_other_task_action_ids_cannot_be_claimed(service):
    observed = observation("directory")
    validation, result = finish(
        service, [observed], [observed["action_id"], str(uuid4())], "查看目录"
    )
    assert validation["completion"]["valid"] is False
    assert result["completion"]["valid"] is False


def test_read_only_goal_uses_actual_read_evidence_while_omitting_auxiliary_reads(service):
    ticket = str(uuid4())
    target = observation("get", params={"ticket_id": ticket})
    auxiliary = observation("directory")
    instruction = json.dumps({"command": "get", "params": {"ticket_id": ticket}})
    _, result = finish(service, [auxiliary, target], [target["action_id"]], instruction)
    assert result["completion"]["valid"] is True
    assert len(result["completion"]["actions"]) == 2


def test_claim_cannot_hide_a_write_behind_a_read_only_goal(service):
    read = observation("get", params={"ticket_id": str(uuid4())})
    write = observation("comment")
    validation, result = finish(service, [read, write], [read["action_id"]], "查看详情")
    assert validation["completion"]["valid"] is False
    assert result["completion"]["valid"] is False


def test_required_target_read_must_have_happened(service):
    auxiliary = observation("directory")
    instruction = json.dumps({"command": "get", "params": {"ticket_id": str(uuid4())}})
    _, result = finish(service, [auxiliary], [auxiliary["action_id"]], instruction)
    assert result["completion"]["valid"] is False


def test_literal_target_resource_must_match_actual_read(service):
    read = observation("get", params={"ticket_id": str(uuid4())})
    instruction = json.dumps({"command": "get", "params": {"ticket_id": str(uuid4())}})
    _, result = finish(service, [read], [read["action_id"]], instruction)
    assert result["completion"]["valid"] is False


def test_required_assignment_must_be_completed(service):
    created = observation("create", ticket=str(uuid4()))
    _, result = finish(service, [created], [created["action_id"]], "创建工单；分配工单给 Chen Li")
    assert result["completion"]["valid"] is False


def test_created_and_assigned_resources_must_be_the_same(service):
    created = observation("create", ticket=str(uuid4()))
    assigned = observation("assign", params={"ticket_id": str(uuid4())})
    observations = [created, assigned]
    _, result = finish(
        service,
        observations,
        [item["action_id"] for item in observations],
        "创建工单；分配工单给 Chen Li",
    )
    assert result["completion"]["valid"] is False


@pytest.mark.parametrize("failure", ["failed", "missing_receipt"])
def test_supplementary_reads_still_require_durable_success(service, failure):
    target, auxiliary = observation("list"), observation("directory")
    service.database = DurableEvidence([target, auxiliary])
    row = service.database.rows[1]
    if failure == "failed":
        row["status"] = "failed"
    else:
        row["receipt"] = None
    result = service._finalize(
        {
            "instruction": "查看工单列表",
            "observations": [target, auxiliary],
            "completion": {"valid": True, "summary": "Model says done"},
        }
    )
    assert result["completion"]["valid"] is False


@pytest.mark.parametrize("command", ["directory", "create"])
def test_unknown_goal_is_not_proven_by_a_successful_registered_action(service, command):
    action = observation(command, ticket=str(uuid4()) if command == "create" else None)
    _, result = finish(service, [action], [action["action_id"]], "帮我登记一下这个网络故障")
    assert result["completion"]["valid"] is False
    assert result["completion"]["reason"] == "goal_unproven"
    assert result["completion"]["summary"] != "Model says done"
    assert result["completion"]["actions"] == [action["action_id"]]


@pytest.mark.parametrize("claim", ["foreign_id", "omitted_write"])
def test_unknown_goal_has_program_failure_when_completion_claim_is_invalid(service, claim):
    observed = [observation("directory"), observation("create", ticket=str(uuid4()))]
    claims = {
        "foreign_id": [*[item["action_id"] for item in observed], str(uuid4())],
        "omitted_write": [observed[0]["action_id"]],
    }
    validation, result = finish(service, observed, claims[claim], "帮我登记一下这个网络故障")
    assert validation["completion"]["valid"] is False
    assert result["completion"]["valid"] is False
    assert result["completion"].get("reason") == "goal_unproven"
    assert result["completion"]["summary"] != "Model says done"
    assert result["completion"]["actions"] == [item["action_id"] for item in observed]
    assert service.database.connections == 0


def test_unregistered_literal_goal_has_program_failure(service):
    action = observation("directory")
    instruction = json.dumps({"command": "unregistered", "params": {}})
    _, result = finish(service, [action], [action["action_id"]], instruction)
    assert result["completion"]["valid"] is False
    assert result["completion"].get("reason") == "goal_unproven"
    assert result["completion"]["summary"] != "Model says done"
    assert result["completion"]["actions"] == [action["action_id"]]


@pytest.mark.parametrize(
    "completion, queue",
    [
        pytest.param(
            {"valid": False, "summary": "Owner rejected the business action"},
            [{"command": "create", "params": {}}],
            id="owner_rejection",
        ),
        pytest.param(
            {"valid": False, "reason": "cancelled", "summary": "Task was cancelled"},
            [],
            id="cancelled",
        ),
    ],
)
def test_unknown_goal_keeps_explicit_program_failure_without_querying_receipts(
    service, completion, queue
):
    action = observation("directory")
    result = service._finalize(
        {
            "instruction": "帮我登记一下这个网络故障",
            "observations": [action],
            "queue": queue,
            "completion": completion,
        }
    )
    assert result["completion"] == {**completion, "actions": [action["action_id"]]}


def test_claim_order_does_not_change_the_outcome(service):
    observations = [observation("directory"), observation("list")]
    ids = [item["action_id"] for item in observations]
    _, original = finish(service, observations, ids, "查看工单列表")
    _, reversed_claim = finish(service, observations, list(reversed(ids)), "查看工单列表")
    assert original == reversed_claim


@pytest.mark.parametrize("kind", ["empty", "duplicate", "invalid_uuid"])
def test_invalid_completion_id_schema_is_rejected(service, kind):
    action = observation("directory")
    claims = {"empty": [], "duplicate": [action["action_id"]] * 2, "invalid_uuid": ["bogus"]}
    with pytest.raises(ValueError):
        finish(service, [action], claims[kind], "查看目录")
