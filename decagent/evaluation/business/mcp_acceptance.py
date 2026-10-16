"""Fixed, offline multi-role acceptance through the real loopback MCP transport.

This suite validates business execution, not planner or Jev decision quality. Its
choices and browser decisions are scripted, and it never contacts a model.
"""

import hashlib
import math
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg.conninfo import conninfo_to_dict

from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.mcp_client import MCPConnectionManager, MCPToolError
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.evaluation.business.dataset import synthetic_directory

SUITE_VERSION = "mcp-multirole-fixed-v1"
SCENARIOS = (
    ("ambiguous_person", "employee-it", "no_write"),
    ("employee_create", "employee-it", "one_write"),
    ("unauthorized_assign", "employee-it", "no_write"),
    ("inaccessible_resource", "employee-facilities", "no_write"),
    ("supervisor_assign", "supervisor", "one_write"),
    ("handler_start", "handler-it", "one_write"),
    ("handler_resolve", "handler-it", "one_write"),
    ("rejected_close", "employee-it", "no_write"),
    ("employee_close", "employee-it", "one_write"),
    ("concurrent_commit", "employee-it", "one_write"),
    ("server_restart_recovery", "employee-it", "one_write"),
)
FROZEN_SUITE_SHA256 = "9018296e85ec87e8498c852b3200c684af276b09a5ae1fff1ce4c4367bc6186b"


@dataclass(repr=False)
class SyntheticActors:
    """Ephemeral credentials for a fresh disposable acceptance database only."""

    credentials: dict[str, tuple[EmployeeIdentity, str]]
    browsers: dict[str, EmployeeIdentity]
    category_id: str
    handler_person_id: str


def provision_synthetic_actors(database, employee_auth):
    """Populate a fresh case_/mcpbench_ database; never seed a user's database."""
    name = conninfo_to_dict(database.url).get("dbname", "")
    if not name.startswith(("case_", "mcpbench_")):
        raise ValueError("MCP acceptance requires a named disposable database")
    directory = synthetic_directory()
    credentials = {}
    browsers = {}
    selected = (
        ("employee-it", "张伟", "IT Support", "employee"),
        ("employee-facilities", "张伟", "Facilities", "employee"),
        ("handler-it", "Zhang Wei", "IT Support", "handler"),
        ("supervisor", "Chen Li", "Network", "supervisor"),
    )
    people = {(p["name"], p["team"]): p["id"] for p in directory["people"]}
    with database.connect() as conn:
        if conn.execute("SELECT count(*) AS n FROM business.people").fetchone()["n"]:
            raise ValueError("MCP acceptance database must be empty")
        if conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"]:
            raise ValueError("MCP acceptance database must have no tickets")
        for person in directory["people"]:
            conn.execute(
                "INSERT INTO business.people(id,name,team,active) VALUES(%s,%s,%s,%s)",
                (person["id"], person["name"], person["team"], person["active"]),
            )
        for category in directory["categories"]:
            conn.execute(
                "INSERT INTO business.categories(id,name) VALUES(%s,%s)",
                (category["id"], category["name"]),
            )
        for login, person_name, team, role in selected:
            employee_id, credential_ref = uuid4(), uuid4()
            person_id = people[person_name, team]
            token = "dcm_" + secrets.token_urlsafe(48)
            browser_token = "dce_" + secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            browser_hash = hashlib.sha256(browser_token.encode()).hexdigest()
            conn.execute(
                "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                "VALUES(%s,%s,%s,%s,'test-only-account-hash')",
                (employee_id, login, person_id, role),
            )
            conn.execute(
                "INSERT INTO business.mcp_credentials"
                "(id,token_hash,employee_id,account_revision,expires_at) "
                "VALUES(%s,%s,%s,1,now()+interval '1 hour')",
                (credential_ref, token_hash, employee_id),
            )
            conn.execute(
                "INSERT INTO business.employee_sessions"
                "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
                "VALUES(%s,%s,1,%s,now()+interval '1 hour')",
                (browser_hash, employee_id, hashlib.sha256(b"synthetic-csrf").hexdigest()),
            )
            credentials[login] = (
                EmployeeIdentity(
                    employee_id,
                    UUID(person_id),
                    login,
                    role,
                    1,
                    "mcp_token",
                    token_hash,
                    credential_ref,
                ),
                token,
            )
            browsers[login] = EmployeeIdentity(
                employee_id, UUID(person_id), login, role, 1, "employee_session", browser_hash
            )
    for identity, token in credentials.values():
        if employee_auth.authenticate_mcp_token(token) != identity:
            raise RuntimeError("Synthetic MCP credential did not authenticate")
    PluginRegistry(database).register(builtin_manifest())
    return SyntheticActors(
        credentials,
        browsers,
        directory["categories"][0]["id"],
        people["Zhang Wei", "IT Support"],
    )


def _percentile(values, fraction):
    """Linear interpolation of ordered observations at (n-1) * fraction."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


class MCPAcceptanceHarness:
    """Run fixed scripted actors against a running modern business MCP server."""

    def __init__(self, database, endpoint_url, employee_auth, actors, *, restart_service=None):
        self.database = database
        self.endpoint_url = endpoint_url
        self.employee_auth = employee_auth
        self.actors = actors
        self.restart_service = restart_service
        self.operations = OperationService(database)
        self.manager = None
        self.clients = {}
        self.latencies = []
        self.browser_decisions = 0
        self.ticket_id = None
        self.operation_ids = []

    def _counts(self):
        with self.database.connect() as conn:
            return tuple(
                conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
                for table in ("tickets", "ticket_history", "operation_receipts")
            )

    def _rpc(self, actor, method, *args, **kwargs):
        began = time.perf_counter()
        try:
            member = getattr(self.clients[actor], method)
            return member(*args, **kwargs) if callable(member) else member
        finally:
            self.latencies.append(time.perf_counter() - began)

    def _prepare_commit(self, actor, command, params, *, request_id):
        prepared = self._rpc(actor, "prepare", request_id, command, params)
        self.operation_ids.append(prepared["operation_id"])
        if prepared["state"] == "awaiting_approval":
            approved = self.operations.reply(
                self.actors.browsers[actor],
                prepared["operation_id"],
                prepared["input_hash"],
                "approve",
            )
            self.browser_decisions += 1
            assert approved["state"] == "ready"
        else:
            assert prepared["state"] == "ready"
        completed = self._rpc(actor, "commit", prepared["operation_id"])
        assert completed["state"] == "succeeded", completed["error_code"]
        assert completed["result"]["ticket"]["id"]
        return completed

    def _ambiguous_person(self):
        data = self._rpc(
            "employee-it", "read", "directory", {"kind": "people", "query": "Zhang Wei"}
        )
        matches = [person for person in data["people"] if person["name"] == "Zhang Wei"]
        assert len(matches) == 2
        assert len({person["team"] for person in matches}) == 2
        return {"candidate_count": len(matches), "action": "clarification_required"}

    def _employee_create(self):
        completed = self._prepare_commit(
            "employee-it",
            "create",
            {"title": "Fixed MCP lifecycle incident", "category_id": self.actors.category_id},
            request_id="acceptance-lifecycle-create",
        )
        ticket = completed["result"]["ticket"]
        self.ticket_id = ticket["id"]
        assert ticket["requester_id"] == str(self.actors.credentials["employee-it"][0].person_id)
        assert ticket["status"] == "open" and ticket["revision"] == 1
        return {"ticket_id": self.ticket_id, "operation_id": completed["operation_id"]}

    def _unauthorized_assign(self):
        try:
            self._rpc(
                "employee-it",
                "prepare",
                "acceptance-forbidden-assign",
                "assign",
                {
                    "ticket_id": self.ticket_id,
                    "expected_revision": 1,
                    "person_id": self.actors.handler_person_id,
                },
            )
        except MCPToolError as exc:
            assert exc.code == "forbidden"
            return {"error_code": exc.code}
        raise AssertionError("Employee assignment unexpectedly succeeded")

    def _inaccessible_resource(self):
        visible = self._rpc("employee-facilities", "read", "list", {})
        assert self.ticket_id not in {ticket["id"] for ticket in visible["tickets"]}
        try:
            self._rpc("employee-facilities", "read", "get", {"ticket_id": self.ticket_id})
        except MCPToolError as exc:
            assert exc.code == "not_found"
            return {"error_code": exc.code}
        raise AssertionError("Another employee read a private ticket")

    def _supervisor_assign(self):
        completed = self._prepare_commit(
            "supervisor",
            "assign",
            {
                "ticket_id": self.ticket_id,
                "expected_revision": 1,
                "person_id": self.actors.handler_person_id,
            },
            request_id="acceptance-lifecycle-assign",
        )
        ticket = completed["result"]["ticket"]
        assert ticket["status"] == "assigned" and ticket["revision"] == 2
        assert ticket["assignee_id"] == self.actors.handler_person_id
        return {"operation_id": completed["operation_id"]}

    def _handler_start(self):
        completed = self._prepare_commit(
            "handler-it",
            "start",
            {"ticket_id": self.ticket_id, "expected_revision": 2},
            request_id="acceptance-lifecycle-start",
        )
        ticket = completed["result"]["ticket"]
        assert ticket["status"] == "in_progress" and ticket["revision"] == 3
        return {"operation_id": completed["operation_id"]}

    def _handler_resolve(self):
        completed = self._prepare_commit(
            "handler-it",
            "resolve",
            {
                "ticket_id": self.ticket_id,
                "expected_revision": 3,
                "resolution": "Synthetic test resolution confirmed by scripted handler",
            },
            request_id="acceptance-lifecycle-resolve",
        )
        ticket = completed["result"]["ticket"]
        assert ticket["status"] == "resolved" and ticket["revision"] == 4
        return {"operation_id": completed["operation_id"]}

    def _rejected_close(self):
        prepared = self._rpc(
            "employee-it",
            "prepare",
            "acceptance-rejected-close",
            "close",
            {"ticket_id": self.ticket_id, "expected_revision": 4},
        )
        assert prepared["state"] == "awaiting_approval"
        rejected = self.operations.reply(
            self.actors.browsers["employee-it"],
            prepared["operation_id"],
            prepared["input_hash"],
            "reject",
        )
        self.browser_decisions += 1
        assert rejected["state"] == "rejected"
        observed = self._rpc("employee-it", "query_operation", prepared["operation_id"])
        assert observed["state"] == "rejected"
        after = self._rpc("employee-it", "read", "get", {"ticket_id": self.ticket_id})
        assert after["ticket"]["status"] == "resolved"
        return {"operation_id": prepared["operation_id"], "state": "rejected"}

    def _employee_close(self):
        completed = self._prepare_commit(
            "employee-it",
            "close",
            {"ticket_id": self.ticket_id, "expected_revision": 4},
            request_id="acceptance-lifecycle-close",
        )
        assert completed["result"]["ticket"]["status"] == "closed"
        read = self._rpc("employee-it", "read", "get", {"ticket_id": self.ticket_id})
        ticket, history = read["ticket"], read["history"]
        assert ticket["revision"] == 5
        assert [row["command"] for row in history] == [
            "create",
            "assign",
            "start",
            "resolve",
            "close",
        ]
        actor_ids = {
            name: str(self.actors.credentials[name][0].employee_id) for name in self.clients
        }
        assert [row["employee_id"] for row in history] == [
            actor_ids[name]
            for name in ("employee-it", "supervisor", "handler-it", "handler-it", "employee-it")
        ]
        assert all(row["source"] == "external_mcp" for row in history)
        return {"ticket_id": self.ticket_id, "history_count": len(history)}

    def _concurrent_commit(self):
        prepared = self._rpc(
            "employee-it",
            "prepare",
            "acceptance-concurrent-create",
            "create",
            {"title": "Concurrent MCP acceptance", "category_id": self.actors.category_id},
        )
        assert prepared["state"] == "ready"
        operation_id = prepared["operation_id"]
        identity, token = self.actors.credentials["employee-it"]

        def commit_once():
            with MCPConnectionManager(self.endpoint_url, self.employee_auth) as manager:
                client = manager.client(identity, token)
                began = time.perf_counter()
                try:
                    return client.commit(operation_id)
                finally:
                    self.latencies.append(time.perf_counter() - began)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(commit_once) for _ in range(2)]
            results = [future.result() for future in futures]
        assert all(row["state"] == "succeeded" for row in results)
        assert results[0]["result"] == results[1]["result"]
        return {"operation_id": operation_id, "identical_results": True}

    def _server_restart_recovery(self):
        prepared = self._rpc(
            "employee-it",
            "prepare",
            "acceptance-recovery-create",
            "create",
            {"title": "Restart recovery acceptance", "category_id": self.actors.category_id},
        )
        assert prepared["state"] == "ready"
        operation_id = prepared["operation_id"]
        # Simulate caller-side reply loss after a completed HTTP commit. This is
        # durable recovery evidence, not a packet-level network fault test.
        self._rpc("employee-it", "commit", operation_id)
        self.manager.close()
        restarted = self.restart_service is not None
        if restarted:
            self.endpoint_url = self.restart_service()
        self.manager = MCPConnectionManager(self.endpoint_url, self.employee_auth)
        identity, token = self.actors.credentials["employee-it"]
        self.clients["employee-it"] = self.manager.client(identity, token)
        observed = self._rpc("employee-it", "query_operation", operation_id)
        replayed = self._rpc("employee-it", "commit", operation_id)
        receipt = self._rpc("employee-it", "read", "receipt", {"operation_id": operation_id})
        assert observed["state"] == replayed["state"] == "succeeded"
        assert observed["result"] == replayed["result"]
        assert receipt["receipt"]["operation_id"] == operation_id
        assert observed["result"]["ticket"]["id"] == receipt["receipt"]["resource_id"]
        return {
            "operation_id": operation_id,
            "recovered": True,
            "response_loss": "simulated_after_completed_http_commit",
            "server_restarted": restarted,
        }

    def run(self):
        """Return a report with a fixed denominator, including any unattempted cases."""
        checksum = hashlib.sha256(repr(SCENARIOS).encode("utf-8")).hexdigest()
        if checksum != FROZEN_SUITE_SHA256:
            raise ValueError("Fixed MCP acceptance suite changed; create a new suite version")
        methods = {
            "ambiguous_person": self._ambiguous_person,
            "employee_create": self._employee_create,
            "unauthorized_assign": self._unauthorized_assign,
            "inaccessible_resource": self._inaccessible_resource,
            "supervisor_assign": self._supervisor_assign,
            "handler_start": self._handler_start,
            "handler_resolve": self._handler_resolve,
            "rejected_close": self._rejected_close,
            "employee_close": self._employee_close,
            "concurrent_commit": self._concurrent_commit,
            "server_restart_recovery": self._server_restart_recovery,
        }
        rows = []
        forbidden_side_effects = 0
        failed = False
        protocol = None
        try:
            self.manager = MCPConnectionManager(self.endpoint_url, self.employee_auth)
            self.clients = {
                name: self.manager.client(*credential)
                for name, credential in self.actors.credentials.items()
            }
            protocol = self._rpc("employee-it", "protocol_version")
            if protocol != "2026-07-28":
                raise AssertionError("Modern MCP protocol required")
            for case_id, actor, effect in SCENARIOS:
                if failed:
                    rows.append(
                        {
                            "id": case_id,
                            "actor": actor,
                            "actor_identity": self.actors.credentials[actor][0].public(),
                            "status": "not_run",
                            "expected_effect": effect,
                        }
                    )
                    continue
                before = self._counts()
                started = time.perf_counter()
                try:
                    evidence = methods[case_id]()
                    after = self._counts()
                    delta = tuple(end - begin for begin, end in zip(before, after, strict=True))
                    expected = (
                        (0, 0, 0)
                        if effect == "no_write"
                        else (
                            (1, 1, 1)
                            if case_id
                            in {"employee_create", "concurrent_commit", "server_restart_recovery"}
                            else (0, 1, 1)
                        )
                    )
                    if effect == "no_write":
                        forbidden_side_effects += sum(max(0, item) for item in delta)
                    assert delta == expected, f"Expected domain effects {expected}, got {delta}"
                    row = {
                        "id": case_id,
                        "actor": actor,
                        "actor_identity": self.actors.credentials[actor][0].public(),
                        "status": "passed",
                        "expected_effect": effect,
                        "domain_delta": delta,
                        "elapsed_seconds": time.perf_counter() - started,
                        "evidence": evidence,
                    }
                except Exception as exc:
                    failed = True
                    after = self._counts()
                    delta = tuple(end - begin for begin, end in zip(before, after, strict=True))
                    if effect == "no_write":
                        forbidden_side_effects += sum(max(0, item) for item in delta)
                    row = {
                        "id": case_id,
                        "actor": actor,
                        "actor_identity": self.actors.credentials[actor][0].public(),
                        "status": "failed",
                        "expected_effect": effect,
                        "domain_delta": delta,
                        "elapsed_seconds": time.perf_counter() - started,
                        "error": type(exc).__name__ + ": " + str(exc),
                    }
                rows.append(row)
        finally:
            if self.manager is not None:
                self.manager.close()
        attempted = sum(row["status"] != "not_run" for row in rows)
        passed = sum(row["status"] == "passed" for row in rows)
        return {
            "schema_version": "mcp-business-acceptance-report-1",
            "suite_version": SUITE_VERSION,
            "suite_sha256": checksum,
            "service_version": "2.0.0",
            "protocol_version": protocol,
            "client": "DecAgent MCPConnectionManager / Python MCP SDK 2.3.0",
            "transport": "real loopback Streamable HTTP",
            "identity_source": "synthetic employee MCP credentials",
            "evidence_mode": "scripted_offline_no_model",
            "model_calls": 0,
            "jev_calls": 0,
            "model_decision_accuracy": None,
            "denominator": len(SCENARIOS),
            "attempted": attempted,
            "passed": passed,
            "failed": attempted - passed,
            "not_run": len(SCENARIOS) - attempted,
            "forbidden_domain_side_effects": forbidden_side_effects,
            "machine_mcp_call_latency_seconds": {
                "count": len(self.latencies),
                "p50": _percentile(self.latencies, 0.5),
                "p95": _percentile(self.latencies, 0.95),
                "sum": sum(self.latencies),
                "percentile_method": "linear interpolation at (n-1)*fraction",
            },
            "scripted_browser_decisions": self.browser_decisions,
            "human_wait_seconds": 0.0,
            "human_wait_note": (
                "No human was present; scripted immediate replies are not user wait measurements"
            ),
            "recovery_scope": (
                "Loopback MCP service restart, durable operation query and receipt replay"
                if self.restart_service is not None
                else "Client reconnect and durable operation query; no server restart"
            ),
            "scenarios": rows,
        }
