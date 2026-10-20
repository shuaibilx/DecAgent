"""Safety gates for the execution-only business-2 compatibility pair."""

import importlib.util
import json
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid5

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "evals" / "benchmarks" / "business2_pair_seed_v1.json"
PAIR_MODULE_PATH = ROOT / "evals" / "benchmarks" / "business2_historical_pair.py"
CHILD_MODULE_PATH = ROOT / "evals" / "benchmarks" / "_business2_pair_child.py"


def _pair_module():
    assert PAIR_MODULE_PATH.is_file(), "the compatibility-pair gate is required"
    spec = importlib.util.spec_from_file_location("business2_historical_pair", PAIR_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child_module():
    assert CHILD_MODULE_PATH.is_file(), "the isolated route runner is required"
    spec = importlib.util.spec_from_file_location("business2_pair_child", CHILD_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_versioned_semantic_seed_has_six_people_and_five_categories():
    assert SEED_PATH.is_file(), "the versioned business-2 pair seed is required"
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    assert seed["schema_version"] == "business2-semantic-seed-v1"
    assert seed["namespace_uuid"] == "2fb6c147-0c0a-4c46-aaf2-5ea71f7c2e85"
    assert len(seed["people"]) == 6
    assert len(seed["categories"]) == 5
    assert all(person["name"] and person["team"] for person in seed["people"])
    assert all(category for category in seed["categories"])


def _identity_permissions(seed, namespace, *, executed=True):
    base = {"close", "comment", "create", "directory", "get", "list", "receipt", "reopen"}
    commands_by_role = {
        "employee": sorted(base),
        "handler": sorted(base | {"start", "resolve"}),
        "supervisor": sorted(base | {"assign", "start", "resolve", "set_priority"}),
    }
    role_commands = {
        account["login"]: commands_by_role[account["role"]] for account in seed["accounts"]
    }
    actor_logins = ("employee-it", "employee-facilities")
    accounts = {account["login"]: account for account in seed["accounts"]}
    actors = []
    for login in actor_logins:
        account = accounts[login]
        actors.append(
            {
                "login": login,
                "employee_id": str(uuid5(namespace, "account:" + login)),
                "person_id": str(uuid5(namespace, account["name"] + ":" + account["team"])),
                "role": account["role"],
                "source": "mcp_token",
                "credential_ref_present": True,
                "allowed_commands": role_commands[login],
            }
        )
    probes = {
        "assign": "forbidden",
        "assign_advertised": False,
        "employee_it_list_before": 0,
        "employee_facilities_list_before": 0,
        "create": "authorized_by_role_policy",
    }
    if executed:
        probes.update({"employee_it_list_after": 1, "employee_facilities_list_after": 0})
    return {"actors": actors, "role_commands": role_commands, "probes": probes}


def _route(side, schema_version):
    module = _pair_module()
    seed = module.load_seed(SEED_PATH)
    namespace = UUID(seed["namespace_uuid"])
    people = [
        {
            "id": str(uuid5(namespace, row["name"] + ":" + row["team"])),
            "name": row["name"],
            "team": row["team"],
            "active": True,
        }
        for row in seed["people"]
    ]
    people.sort(key=lambda row: (row["name"], row["team"], row["id"]))
    categories = [
        {
            "id": str(uuid5(namespace, "category:" + name)),
            "name": name,
        }
        for name in seed["categories"]
    ]
    categories.sort(key=lambda row: (row["name"], row["id"]))
    state_digest = module.sha256(
        module.canonical({"people": people, "categories": categories}).encode()
    )
    case = module.resolve_case(seed)
    case_payload = {
        key: case[key]
        for key in ("case_id", "command", "params", "expected_requester_id", "instruction")
    }
    expected_ticket = {
        **case["params"],
        "requester_id": case["expected_requester_id"],
        "employee_id": str(uuid5(namespace, "account:employee-it")),
        "status": "open",
    }
    route = {
        "route": "execution_only",
        "fast_path": "hybrid_literal" if side == "historical" else "literal_plan",
        "model_calls": 0,
        "jev_calls": 0,
        "source": {
            "source_hashes": {"actions.py": "b" * 64, "grants.py": "c" * 64},
            **({"archive_sha256": module.ARCHIVE_SHA256} if side == "historical" else {}),
        },
        "database": {
            "name": side + "_db",
            "schema_version": schema_version,
            "process_id": 100 if side == "historical" else 101,
        },
        "seed": {
            "version": "business2-semantic-seed-v1",
            "people_rows": 6,
            "categories_rows": 5,
            "people": people,
            "categories": categories,
            "state_digest": state_digest,
            "verified_from_database": True,
        },
        "case": case_payload,
        "case_digest": case["case_digest"],
        "identity_permissions": _identity_permissions(seed, namespace),
        "tools": {
            "handshake_completed": True,
            "protocol_version": "2026-07-28",
            "schemas": [{"name": "tickets_prepare_create", "inputSchema": {"type": "object"}}],
        },
        "governance": {
            "assessment": {
                "outcome": "allow",
                "reason": "Explicit requested write satisfies the current plugin contract",
                "policy_version": "business-approval-v2",
                "resource": None,
            },
            "effective_params": {
                **case["params"],
                "requester_id": case["expected_requester_id"],
            },
            "persisted_action": True,
        },
        "execution": {
            "status": "succeeded",
            "task_state": "succeeded",
            "ticket_rows": 1,
            "business_row_counts": {
                "tickets": 1,
                "ticket_history": 1,
                "operation_receipts": 2,
            },
            "ticket": expected_ticket,
            "employee_it_visible_tickets": 1,
            "employee_facilities_visible_tickets": 0,
            "write_action_count": 1,
            "requester_matches_seed": True,
            "elapsed_seconds": 0.25,
        },
    }
    _seal_route(route)
    return route


def _digest(value):
    module = _pair_module()
    return module.sha256(module.canonical(value).encode("utf-8"))


def _seal_route(route, *, preflight=False):
    source = route["source"]
    source.pop("provenance_digest", None)
    source["provenance_digest"] = _digest(source)
    identity = route["identity_permissions"]
    identity.pop("digest", None)
    identity["digest"] = _digest(identity)
    tools = route["tools"]
    tools["schema_digest"] = _digest(tools["schemas"])
    governance = route["governance"]
    if preflight:
        governance.pop("digest", None)
        governance["preflight_digest"] = _digest(
            {
                "assessment": governance["assessment"],
                "effective_params": governance["effective_params"],
            }
        )
    else:
        governance.pop("preflight_digest", None)
        governance["digest"] = _digest(
            {
                "assessment": governance["assessment"],
                "effective_params": governance["effective_params"],
            }
        )


def test_pair_gate_allows_only_matching_execution_evidence_with_separate_boundaries():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "execution_only_eligible"
    assert result["performance_evidence_eligible"] is True
    assert result["scope"] == "execution_only"


def test_pair_gate_fails_closed_when_database_seed_is_empty():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    historical["seed"]["people_rows"] = 0
    historical["seed"]["categories_rows"] = 0

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert result["performance_evidence_eligible"] is False
    assert "historical_seed_not_nonempty" in result["reasons"]


def test_pair_gate_fails_closed_when_wire_schemas_differ():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    current["tools"]["schemas"][0]["inputSchema"]["required"] = ["request_id"]

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "tool_schemas_differ" in result["reasons"]


def test_pair_gate_fails_closed_when_identity_or_governance_differ():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    current["identity_permissions"]["probes"]["assign"] = "allowed"
    current["governance"]["effective_params"]["requester_id"] = "person-2"

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "identity_permissions_differ" in result["reasons"]
    assert "governance_evidence_differ" in result["reasons"]


def test_pair_gate_requires_distinct_db_processes_and_zero_model_calls():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    current["database"]["process_id"] = historical["database"]["process_id"]
    current["model_calls"] = 1

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "process_boundaries_not_distinct" in result["reasons"]
    assert "model_calls_nonzero" in result["reasons"]


def test_execution_gate_requires_a_persisted_governance_action():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    current["governance"]["persisted_action"] = False

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "current_persisted_action_missing" in result["reasons"]


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("source", "historical_source_provenance_digest_mismatch"),
        ("identity", "historical_identity_permissions_digest_mismatch"),
        ("tools", "historical_tool_schema_digest_mismatch"),
        ("governance", "historical_governance_digest_mismatch"),
        ("case", "historical_case_digest_mismatch"),
    ],
)
def test_pair_gate_recomputes_all_evidence_digests(tamper, reason):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    if tamper == "source":
        historical["source"]["provenance_digest"] = "0" * 64
    elif tamper == "identity":
        historical["identity_permissions"]["digest"] = "0" * 64
    elif tamper == "tools":
        historical["tools"]["schema_digest"] = "0" * 64
    elif tamper == "governance":
        historical["governance"]["digest"] = "0" * 64
    else:
        historical["case_digest"] = "0" * 64
        current["case_digest"] = "0" * 64

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert reason in result["reasons"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("task_state", "running", "historical_task_state_not_succeeded"),
        ("ticket_rows", 0, "historical_ticket_rows_invalid"),
        ("business_row_counts", {"tickets": 1}, "historical_business_row_counts_invalid"),
        ("write_action_count", 0, "historical_write_action_count_invalid"),
        ("requester_matches_seed", False, "historical_requester_seed_mismatch"),
        ("employee_it_visible_tickets", 0, "historical_employee_it_visibility_invalid"),
        (
            "employee_facilities_visible_tickets",
            1,
            "historical_employee_facilities_visibility_invalid",
        ),
    ],
)
def test_execution_gate_requires_complete_persisted_state(field, value, reason):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    historical["execution"][field] = value

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert reason in result["reasons"]


def test_execution_gate_requires_ticket_requester_to_match_frozen_case():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    historical["execution"]["ticket"]["requester_id"] = "someone-else"

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "historical_ticket_requester_mismatch" in result["reasons"]


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("title", "Other ticket title"),
        ("description", "Other description"),
        ("category_id", "missing-category-id"),
        ("priority", "urgent"),
        ("requester_id", "someone-else"),
        ("employee_id", "someone-else"),
        ("status", "closed"),
    ],
)
def test_execution_gate_requires_frozen_ticket_business_fields(field, wrong_value):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    historical["execution"]["ticket"][field] = wrong_value

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "historical_ticket_business_fields_mismatch" in result["reasons"]


def test_execution_gate_requires_both_ticket_business_projections_to_match():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    historical["execution"]["ticket"]["id"] = "ticket-id-historical"
    current["execution"]["ticket"]["id"] = "ticket-id-current"

    assert module.evaluate_pair(historical, current)["status"] == "execution_only_eligible"

    current["execution"]["ticket"]["priority"] = "low"
    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "ticket_business_projections_differ" in result["reasons"]


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("outcome", "historical_governance_outcome_not_allow"),
        ("resource", "historical_governance_resource_mismatch"),
        ("empty_reason", "historical_governance_reason_missing"),
        ("empty_policy", "historical_governance_policy_version_missing"),
        ("wrong_params", "historical_effective_params_case_mismatch"),
        ("extra_params", "historical_effective_params_case_mismatch"),
    ],
)
def test_gate_rejects_matching_but_semantically_invalid_governance_after_resealing(
    tamper, reason
):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    for route in (historical, current):
        governance = route["governance"]
        if tamper == "outcome":
            governance["assessment"]["outcome"] = "deny"
        elif tamper == "resource":
            governance["assessment"]["resource"] = {"id": "unrelated-resource"}
        elif tamper == "empty_reason":
            governance["assessment"]["reason"] = "  "
        elif tamper == "empty_policy":
            governance["assessment"]["policy_version"] = ""
        elif tamper == "wrong_params":
            governance["effective_params"]["title"] = "Different title"
        else:
            governance["effective_params"]["unexpected"] = "extra"
        _seal_route(route)

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert reason in result["reasons"]
    assert not any("governance_digest" in item for item in result["reasons"])


@pytest.mark.parametrize("field", ["reason", "policy_version"])
def test_gate_requires_governance_reason_and_policy_to_match_between_routes(field):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    current["governance"]["assessment"][field] += " changed"
    _seal_route(historical)
    _seal_route(current)

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert "governance_evidence_differ" in result["reasons"]
    assert not any("governance_digest" in item for item in result["reasons"])


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("assign", "historical_identity_probe_mismatch"),
        ("assign_advertised", "historical_identity_probe_mismatch"),
        ("employee_it_list_before", "historical_identity_probe_mismatch"),
        ("employee_facilities_list_before", "historical_identity_probe_mismatch"),
        ("create", "historical_identity_probe_mismatch"),
        ("employee_it_list_after", "historical_identity_probe_mismatch"),
        ("employee_facilities_list_after", "historical_identity_probe_mismatch"),
    ],
)
def test_gate_rejects_invalid_permission_probes_after_resealing(tamper, reason):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    wrong_values = {
        "assign": "allowed",
        "assign_advertised": True,
        "employee_it_list_before": 1,
        "employee_facilities_list_before": 1,
        "create": "allowed",
        "employee_it_list_after": 0,
        "employee_facilities_list_after": 1,
    }
    for route in (historical, current):
        route["identity_permissions"]["probes"][tamper] = wrong_values[tamper]
        _seal_route(route)

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert reason in result["reasons"]
    assert not any("identity_permissions_digest_mismatch" in item for item in result["reasons"])


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("missing_actor", "historical_identity_actors_mismatch"),
        ("employee_id", "historical_identity_actors_mismatch"),
        ("person_id", "historical_identity_actors_mismatch"),
        ("role", "historical_identity_actors_mismatch"),
        ("actor_permissions", "historical_identity_actors_mismatch"),
        ("role_permissions", "historical_identity_role_permissions_mismatch"),
    ],
)
def test_gate_rejects_incorrect_seed_identity_or_role_permissions_after_resealing(
    tamper, reason
):
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    for route in (historical, current):
        identity = route["identity_permissions"]
        employee = next(actor for actor in identity["actors"] if actor["login"] == "employee-it")
        if tamper == "missing_actor":
            identity["actors"] = [
                actor for actor in identity["actors"] if actor["login"] != "employee-facilities"
            ]
        elif tamper in {"employee_id", "person_id", "role"}:
            employee[tamper] = "incorrect-value"
        elif tamper == "actor_permissions":
            employee["allowed_commands"].append("assign")
            employee["allowed_commands"].sort()
        else:
            identity["role_commands"]["employee-it"].append("assign")
            identity["role_commands"]["employee-it"].sort()
        _seal_route(route)

    result = module.evaluate_pair(historical, current)

    assert result["status"] == "compatibility_only/ineligible"
    assert reason in result["reasons"]
    assert not any("identity_permissions_digest_mismatch" in item for item in result["reasons"])


def test_preflight_gate_checks_preflight_digest_without_final_execution_fields():
    module = _pair_module()
    historical = _route("historical", 11)
    current = _route("current", 17)
    for route in (historical, current):
        route["execution"] = {"status": "pending"}
        route["governance"]["persisted_action"] = False
        route["identity_permissions"]["probes"].pop("employee_it_list_after")
        route["identity_permissions"]["probes"].pop("employee_facilities_list_after")
        _seal_route(route, preflight=True)

    result = module.evaluate_pair(historical, current, require_execution=False)

    assert result["status"] == "preflight_eligible"
    historical["governance"]["preflight_digest"] = "0" * 64
    rejected = module.evaluate_pair(historical, current, require_execution=False)
    assert rejected["status"] == "compatibility_only/ineligible"
    assert "historical_governance_preflight_digest_mismatch" in rejected["reasons"]


def test_child_argv_excludes_database_url_and_bootstrap_precedes_control_line():
    pair = _pair_module()
    child = _child_module()
    database_url = "postgresql://test-user:test-password@127.0.0.1:11640/test_db"
    command = pair._child_command(
        "current",
        Path("C:/isolated/current-source"),
        Path("C:/repo/evals/benchmarks/business2_pair_seed_v1.json"),
        pair.ARCHIVE_SHA256,
        Path("C:/scratch"),
    )
    assert "--database-url" not in command
    assert database_url not in command

    class Process:
        stdin = StringIO()

    process = Process()
    pair._send_database_bootstrap(process, database_url)
    bootstrap_line = process.stdin.getvalue()
    assert json.loads(bootstrap_line) == {"database_url": database_url}
    control_stream = StringIO(bootstrap_line + '{"action":"execute"}\n')
    assert child._read_database_bootstrap(control_stream) == database_url
    assert json.loads(control_stream.readline()) == {"action": "execute"}


def test_second_child_start_failure_kills_and_waits_for_first(monkeypatch):
    pair = _pair_module()

    class Process:
        killed = False
        waited = False

        def poll(self):
            return None if not self.waited else -9

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert self.killed
            self.waited = True
            return -9

    first = Process()
    calls = 0

    def start_child(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first, object(), []
        raise OSError("simulated second spawn failure")

    monkeypatch.setattr(pair, "_start_child", start_child)
    child_specs = [
        ("historical", Path("archive"), "unused", Path("seed"), "hash", Path("scratch")),
        ("current", Path("repo"), "unused", Path("seed"), "hash", Path("scratch")),
    ]

    with pytest.raises(OSError, match="simulated second spawn failure"):
        with pair._started_children(child_specs):
            pytest.fail("startup should not yield when the second child fails")

    assert first.killed is True
    assert first.waited is True


def test_child_reader_start_failure_kills_waits_and_joins_started_reader(monkeypatch):
    pair = _pair_module()

    class Process:
        stdin = StringIO()
        stdout = StringIO()
        stderr = StringIO()
        killed = False
        waited = False

        def poll(self):
            return None if not self.waited else -9

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert self.killed
            self.waited = True
            return -9

    process = Process()
    monkeypatch.setattr(pair.subprocess, "Popen", lambda *args, **kwargs: process)
    readers = []

    class ReaderThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False
            self.joined = False
            readers.append(self)

        def start(self):
            if len(readers) == 2:
                raise RuntimeError("simulated second reader start failure")
            self.started = True

        def join(self, timeout=None):
            self.joined = True

    monkeypatch.setattr(pair.threading, "Thread", ReaderThread)

    with pytest.raises(RuntimeError, match="simulated second reader start failure"):
        pair._start_child(
            "current",
            Path("source"),
            "unused-database-url",
            Path("seed"),
            "archive-hash",
            Path("scratch"),
        )

    assert process.killed is True
    assert process.waited is True
    assert readers[0].started is True
    assert readers[0].joined is True


def test_persisted_assessment_normalizes_policy_and_resource_from_action_snapshot():
    child = _child_module()
    result = child._persisted_assessment(
        {
            "policy_version": "business-approval-v2",
            "resource": {"id": "ticket-1", "revision": 2},
            "assessment": {
                "outcome": "allow",
                "reason": "Explicit write",
                "digest": "a" * 64,
            },
        }
    )

    assert result == {
        "outcome": "allow",
        "reason": "Explicit write",
        "policy_version": "business-approval-v2",
        "resource": {"id": "ticket-1", "revision": 2},
    }
