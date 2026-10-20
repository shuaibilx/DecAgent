"""Run one side of the isolated historical/current business pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid5


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_database_bootstrap(stream):
    line = stream.readline()
    try:
        bootstrap = json.loads(line) if line else None
    except json.JSONDecodeError as error:
        raise ValueError("Missing or invalid database bootstrap message") from error
    database_url = bootstrap.get("database_url") if isinstance(bootstrap, dict) else None
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("Missing or invalid database bootstrap message")
    return database_url


def _json(value):
    from datetime import date, datetime

    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _guard_model_calls(counters):
    from decagent.decisions.adapter import JevAdapter
    from decagent.models.primary import PrimaryModel

    def denied_primary(self, *args, **kwargs):
        counters["model_calls"] += 1
        raise RuntimeError("Model dispatch is forbidden in this execution-only pair")

    def denied_jev(self, *args, **kwargs):
        counters["jev_calls"] += 1
        raise RuntimeError("Jev dispatch is forbidden in this execution-only pair")

    PrimaryModel.respond = denied_primary
    JevAdapter.evaluate = denied_jev


def _source_evidence(source_root, archive_sha256, side):
    names = (
        "decagent/business/actions.py",
        "decagent/business/access.py",
        "decagent/business/execution.py",
        "decagent/business/grants.py",
        "decagent/business/mcp_contract.py",
        "decagent/business/mcp_server.py",
        "decagent/business/mcp_client.py",
        "decagent/business/tickets/store.py",
        "decagent/business/worker.py",
        "decagent/business/services.py",
        "decagent/business/conditional_services.py",
        "decagent/business/plans/runtime.py",
        "decagent/business/plans/handlers.py",
    )
    hashes = {}
    for name in names:
        path = source_root / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"source_hashes": hashes}
    if side == "historical":
        result["archive_sha256"] = archive_sha256
    result["provenance_digest"] = _digest(result)
    return result


def _seed_database(database, seed, namespace):
    from decagent.business.tickets.store import TicketStore

    result = TicketStore(database.url).seed_directory("business2-pair-seed-v1")
    if not result.get("ok"):
        raise RuntimeError("Versioned directory seed failed")
    with database.connect() as conn:
        people = conn.execute(
            "SELECT id,name,team,active FROM business.people ORDER BY name,team,id"
        ).fetchall()
        categories = conn.execute(
            "SELECT id,name FROM business.categories ORDER BY name,id"
        ).fetchall()
    actual_people = _json(people)
    actual_categories = _json(categories)
    expected_people = [
        {
            "id": str(uuid5(namespace, row["name"] + ":" + row["team"])),
            "name": row["name"],
            "team": row["team"],
            "active": True,
        }
        for row in seed["people"]
    ]
    expected_people.sort(key=lambda row: (row["name"], row["team"], row["id"]))
    expected_categories = [
        {"id": str(uuid5(namespace, "category:" + name)), "name": name}
        for name in seed["categories"]
    ]
    expected_categories.sort(key=lambda row: (row["name"], row["id"]))
    return {
        "version": seed["schema_version"],
        "people_rows": len(actual_people),
        "categories_rows": len(actual_categories),
        "people": actual_people,
        "categories": actual_categories,
        "state_digest": _digest({"people": actual_people, "categories": actual_categories}),
        "seed_sha256": seed["seed_sha256"],
        "verified_from_database": actual_people == expected_people
        and actual_categories == expected_categories,
    }


def _setup_auth(database, source_root, scratch, seed, namespace):
    from decagent.business.employee_auth import EmployeeAuth, EmployeeIdentity

    accounts = {row["login"]: row for row in seed["accounts"]}
    with database.connect() as conn:
        people = {
            (row["name"], row["team"]): row["id"]
            for row in conn.execute("SELECT id,name,team FROM business.people").fetchall()
        }
        account_ids = {}
        for account in seed["accounts"]:
            login = account["login"]
            employee_id = uuid5(namespace, "account:" + login)
            account_ids[login] = employee_id
            conn.execute(
                "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                "VALUES(%s,%s,%s,%s,'pair-only-synthetic-password-hash')",
                (
                    employee_id,
                    login,
                    people[(account["name"], account["team"])],
                    account["role"],
                ),
            )
    auth = EmployeeAuth(
        database,
        "http://127.0.0.1:8000",
        False,
        scratch / "credentials",
    )
    issued = {}
    for login in ("employee-it", "employee-facilities"):
        account = accounts[login]
        employee_id = account_ids[login]
        person_id = uuid5(namespace, account["name"] + ":" + account["team"])
        session_token = "dce_pair_" + uuid5(namespace, "session:" + login).hex
        session_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO business.employee_sessions"
                "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
                "VALUES(%s,%s,1,%s,now()+interval '1 hour')",
                (session_hash, employee_id, "a" * 64),
            )
        browser = EmployeeIdentity(
            employee_id,
            person_id,
            login,
            account["role"],
            1,
            "employee_session",
            session_hash,
        )
        credential = auth.issue_mcp(browser)
        token = Path(credential["credential_path"]).read_text(encoding="utf-8").strip()
        identity = auth.authenticate_mcp_token(token)
        if identity is None:
            raise RuntimeError("Issued MCP credential did not authenticate")
        issued[login] = {"token": token, "identity": identity, "credential": credential}
    return auth, issued, account_ids


def _start_server(database, auth):
    import uvicorn
    from decagent.business.mcp_server import create_mcp_app

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_mcp_app(database, auth, port=port)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
        name="business2-pair-loopback-mcp",
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        listener.close()
        thread.join(5)
        raise RuntimeError("Loopback MCP server failed to start")
    return server, thread, listener, f"http://127.0.0.1:{port}/mcp"


def _normalized_assessment(assessment):
    snapshot = assessment.get("snapshot", {}) if isinstance(assessment, dict) else {}
    return _json(
        {
            "outcome": assessment.get("outcome"),
            "reason": assessment.get("reason"),
            "policy_version": assessment.get("policy_version") or snapshot.get("policy_version"),
            "resource": assessment.get("resource", snapshot.get("resource")),
        }
    )


def _persisted_assessment(action_snapshot):
    saved = action_snapshot.get("assessment", {})
    if not isinstance(saved, dict):
        saved = {}
    return _normalized_assessment(
        {
            **saved,
            "policy_version": action_snapshot.get("policy_version"),
            "resource": action_snapshot.get("resource"),
        }
    )


def _build_plan(spec, case):
    from decagent.business.plans.templates import QuestionRegistry

    params = case["params"]
    receipt_source = {
        "source": "node_output",
        "node_id": "create",
        "path": ["receipt", "operation_id"],
        "value_type": "string",
        "nullable": False,
        "provenance": "write_receipt",
    }
    inputs = {
        key: {
            "source": "literal",
            "value": value,
            "value_type": "string",
            "nullable": False,
            "provenance": "plan_literal",
        }
        for key, value in params.items()
    }
    return {
        "schema_version": "1",
        "plan_id": "business2-pair-create-v1",
        "revision": 1,
        "registry_fingerprint": QuestionRegistry.from_spec(spec).fingerprint,
        "entry": "create",
        "goal": {
            "description": "Create the fixed execution-only compatibility case",
            "evidence": [
                {
                    "id": "created",
                    "kind": "write_receipt",
                    "description": "The committed create receipt",
                    "source": receipt_source,
                }
            ],
        },
        "nodes": [
            {
                "id": "create",
                "type": "governed_write",
                "depends_on": [],
                "on_missing": {"stop": "missing_input"},
                "on_error": {"stop": "error"},
                "command": {"plugin_id": spec.id, "command": "create"},
                "inputs": inputs,
                "next": ["done"],
            },
            {
                "id": "done",
                "type": "complete",
                "depends_on": ["create"],
                "on_missing": {"stop": "missing_input"},
                "on_error": {"stop": "error"},
                "evidence": ["created"],
            },
        ],
    }


def _identity_permissions(database, issued, account_ids):
    from decagent.business import access

    actors = []
    for login in ("employee-it", "employee-facilities"):
        identity = issued[login]["identity"]
        with database.connect() as conn:
            commands = sorted(access.allowed_commands(conn, identity.employee_id))
        actors.append(
            {
                "login": identity.login,
                "employee_id": str(identity.employee_id),
                "person_id": str(identity.person_id),
                "role": identity.role,
                "source": identity.source,
                "credential_ref_present": identity.credential_ref is not None,
                "allowed_commands": commands,
            }
        )
    with database.connect() as conn:
        role_commands = {}
        for login, employee_id in account_ids.items():
            role_commands[login] = sorted(access.allowed_commands(conn, employee_id))
    return actors, role_commands


def _preflight(args, database, seed, namespace, source_root, archive_sha256, scratch, counters):
    from decagent.business import access
    from decagent.business.actions import assess_action
    from decagent.business.mcp_client import MCPConnectionManager, MCPToolError
    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest
    from decagent.decisions.contracts import digest as decision_digest
    from decagent.storage.database import SCHEMA_VERSION

    database.migrate()
    with database.connect() as conn:
        schema_rows = conn.execute(
            "SELECT version FROM app.schema_migrations ORDER BY version"
        ).fetchall()
    schema_version = schema_rows[-1]["version"]
    expected_schema = 11 if args.side == "historical" else 17
    if schema_version != expected_schema or schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Unexpected schema version: expected {expected_schema}, observed {schema_version}"
        )
    seed_evidence = _seed_database(database, seed, namespace)
    PluginRegistry(database).register(builtin_manifest())
    scope = PluginRegistry(database).list()[0]
    auth, issued, account_ids = _setup_auth(database, source_root, scratch, seed, namespace)
    server, thread, listener, endpoint = _start_server(database, auth)
    manager = MCPConnectionManager(endpoint, auth)
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": args.case["instruction"],
            "strategy": "hybrid",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {
                "employee_id": str(issued["employee-it"]["identity"].employee_id),
                "authenticated": True,
            },
        },
        graph_version="business-2" if args.side == "historical" else "business-3",
        employee_id=issued["employee-it"]["identity"].employee_id,
        credential_ref=UUID(issued["employee-it"]["credential"]["credential_ref"]),
    )
    clients = {
        login: manager.client(item["identity"], item["token"]) for login, item in issued.items()
    }
    client = clients["employee-it"]
    protocol_version = client.protocol_version
    schemas = [_json(schema) for schema in client.list_tools()]
    directory = client.read("directory", {})
    own_before = client.read("list", {})
    peer_before = clients["employee-facilities"].read("list", {})
    try:
        client.prepare(
            "pair-permission-probe",
            "assign",
            {
                "ticket_id": str(uuid5(namespace, "permission-probe:ticket")),
                "expected_revision": 1,
                "person_id": str(uuid5(namespace, "permission-probe:person")),
            },
        )
        assign_probe = "allowed"
    except MCPToolError as error:
        assign_probe = error.code
    except Exception as error:
        assign_probe = "tool_unavailable:" + type(error).__name__
    actors, role_commands = _identity_permissions(database, issued, account_ids)
    case = args.case
    instruction = case["instruction"]
    with database.connect() as conn:
        effective_params = access.authorize_write(
            conn,
            issued["employee-it"]["identity"].employee_id,
            case["command"],
            case["params"],
        )
    evidence = {
        "directory": directory,
        "sources": {"directory": decision_digest(directory)},
    }
    assessment = assess_action(
        PluginRegistry(database).get(scope["id"], scope["manifest_digest"]),
        str(task["id"]),
        str(scope["id"]),
        instruction,
        case["command"],
        case["params"],
        evidence,
    )
    identity_projection = {
        "actors": actors,
        "role_commands": role_commands,
        "probes": {
            "assign": assign_probe,
            "assign_advertised": any(
                item.get("name") == "tickets_prepare_assign" for item in schemas
            ),
            "employee_it_list_before": len(own_before.get("tickets", [])),
            "employee_facilities_list_before": len(peer_before.get("tickets", [])),
            "create": "authorized_by_role_policy",
        },
    }
    identity_projection["digest"] = _digest(identity_projection)
    tool_evidence = {
        "handshake_completed": True,
        "protocol_version": protocol_version,
        "schemas": schemas,
        "schema_digest": _digest(schemas),
    }
    governance = {
        "assessment": _normalized_assessment(assessment),
        "effective_params": _json(effective_params),
        "preflight_digest": _digest(
            {
                "assessment": _normalized_assessment(assessment),
                "effective_params": _json(effective_params),
            }
        ),
        "persisted_action": False,
    }
    with database.connect() as conn:
        database_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    route = {
        "route": "execution_only",
        "fast_path": "hybrid_literal" if args.side == "historical" else "literal_plan",
        "model_calls": counters["model_calls"],
        "jev_calls": counters["jev_calls"],
        "source": _source_evidence(source_root, archive_sha256, args.side),
        "database": {
            "name": database_name,
            "schema_version": schema_version,
            "process_id": os.getpid(),
        },
        "seed": seed_evidence,
        "case": {
            key: case[key]
            for key in (
                "case_id",
                "command",
                "params",
                "expected_requester_id",
                "instruction",
            )
        },
        "case_digest": case["case_digest"],
        "identity_permissions": identity_projection,
        "tools": tool_evidence,
        "governance": governance,
        "execution": {"status": "pending"},
    }
    if not seed_evidence["verified_from_database"]:
        route["seed"]["state_digest"] = ""
    return route, {
        "database": database,
        "auth": auth,
        "issued": issued,
        "clients": clients,
        "manager": manager,
        "server": server,
        "thread": thread,
        "listener": listener,
        "task": task,
        "scope": scope,
        "namespace": namespace,
        "seed": seed,
        "case": case,
        "scratch": scratch,
    }


def _execute(args, state, route, counters):
    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig

    database = state["database"]
    task = state["task"]
    owner = "business2-pair-" + args.side
    settings = Settings(database_url=database.url, data_dir=state["scratch"] / "data")
    runtime_config = RuntimeConfig(tickets_mcp_url=state["endpoint"])
    started = time.monotonic()
    if args.side == "historical":
        from decagent.business.worker import BusinessWorker

        worker = BusinessWorker(
            database,
            settings,
            runtime_config,
            owner=owner,
            employee_auth=state["auth"],
            mcp_manager=state["manager"],
            heartbeat_seconds=1,
            lease_seconds=30,
        )
        if not worker.run_once():
            raise RuntimeError("Historical BusinessWorker did not claim the fixed task")
        worker.close()
        final_task = database.get_task(task["id"])
        execution_status = "succeeded" if final_task["state"] == "succeeded" else "failed"
    else:
        from decagent.business.conditional_services import ConditionalBusinessServices

        claimed = database.claim(task["id"], owner, seconds=60)
        if not claimed:
            raise RuntimeError("Current PlanRuntime task lease could not be acquired")
        issued = state["issued"]["employee-it"]
        client = state["manager"].client(issued["identity"], issued["token"])
        services = ConditionalBusinessServices(
            database,
            settings,
            runtime_config,
            claimed,
            owner,
            claimed["lease_generation"],
            mcp_client=client,
        )
        services.repository.publish(_build_plan(services.spec, state["case"]))
        runtime = services._runtime()
        result = None
        for _ in range(12):
            result = runtime.step()
            if result["status"] != "advanced":
                break
        if (
            not result
            or result.get("status") != "complete"
            or not result.get("completion", {}).get("valid")
        ):
            raise RuntimeError("Production PlanRuntime did not complete the fixed plan")
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET state='succeeded',phase='finalize',lease_owner=NULL,"
                "lease_until=NULL WHERE id=%s AND lease_owner=%s AND lease_generation=%s",
                (task["id"], owner, claimed["lease_generation"]),
            )
        services.close()
        execution_status = "succeeded"
    elapsed = time.monotonic() - started
    identity = state["issued"]["employee-it"]["identity"]
    own_after = state["clients"]["employee-it"].read("list", {})
    peer_after = state["clients"]["employee-facilities"].read("list", {})
    with database.connect() as conn:
        tickets = conn.execute(
            "SELECT id,title,description,category_id,requester_id,employee_id,priority,status "
            "FROM business.tickets WHERE title=%s ORDER BY id",
            (state["case"]["params"]["title"],),
        ).fetchall()
        counts = {
            table: conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        }
        actions = conn.execute(
            "SELECT id,status,snapshot,server_operation_id,receipt FROM app.actions "
            "WHERE task_id=%s ORDER BY created_at,id",
            (task["id"],),
        ).fetchall()
    persisted = actions[0] if len(actions) == 1 else None
    assessment = {}
    effective_params = {}
    if persisted:
        snapshot = persisted["snapshot"] or {}
        assessment = _persisted_assessment(snapshot)
        effective_params = _json(snapshot.get("effective_params") or snapshot.get("params") or {})
        if persisted["server_operation_id"]:
            with database.connect() as conn:
                operation = conn.execute(
                    "SELECT effective_params FROM business.operations WHERE id=%s",
                    (persisted["server_operation_id"],),
                ).fetchone()
            if operation and operation["effective_params"] is not None:
                effective_params = _json(operation["effective_params"])
    route["execution"] = {
        "status": execution_status,
        "elapsed_seconds": round(elapsed, 6),
        "task_state": database.get_task(task["id"])["state"],
        "ticket_rows": len(tickets),
        "business_row_counts": counts,
        "ticket": _json(tickets[0]) if len(tickets) == 1 else None,
        "employee_it_visible_tickets": len(own_after.get("tickets", [])),
        "employee_facilities_visible_tickets": len(peer_after.get("tickets", [])),
    }
    route["identity_permissions"]["probes"].update(
        {
            "employee_it_list_after": len(own_after.get("tickets", [])),
            "employee_facilities_list_after": len(peer_after.get("tickets", [])),
        }
    )
    route["identity_permissions"].pop("digest", None)
    route["identity_permissions"]["digest"] = _digest(route["identity_permissions"])
    route["governance"].update(
        {
            "assessment": assessment,
            "effective_params": effective_params,
            "persisted_action": persisted is not None and persisted["status"] == "succeeded",
            "action_id": str(persisted["id"]) if persisted else None,
            "server_operation_id": persisted["server_operation_id"] if persisted else None,
        }
    )
    route["governance"].pop("preflight_digest", None)
    route["governance"]["digest"] = _digest(
        {
            "assessment": route["governance"]["assessment"],
            "effective_params": route["governance"]["effective_params"],
        }
    )
    route["model_calls"] = counters["model_calls"]
    route["jev_calls"] = counters["jev_calls"]
    route["execution"]["write_action_count"] = len(actions)
    route["execution"]["requester_matches_seed"] = bool(
        len(tickets) == 1
        and str(tickets[0]["requester_id"]) == state["case"]["expected_requester_id"]
        and str(tickets[0]["employee_id"]) == str(identity.employee_id)
    )
    state["manager"].close()
    state["server"].should_exit = True
    state["thread"].join(10)
    state["listener"].close()
    if state["thread"].is_alive():
        raise RuntimeError("Loopback MCP server did not stop cleanly")
    return route


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=("historical", "current"), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--seed-path", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    database_url = _read_database_bootstrap(sys.stdin)
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    sys.path.insert(1, str(Path(__file__).resolve().parent))
    from decagent.storage.database import Database

    from business2_historical_pair import load_seed, resolve_case

    seed = load_seed(args.seed_path)
    namespace = UUID(seed["namespace_uuid"])
    args.case = resolve_case(seed)
    counters = {"model_calls": 0, "jev_calls": 0}
    _guard_model_calls(counters)
    database = Database(database_url)
    scratch = args.scratch_dir.resolve() / f"{args.side}-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=False)
    route, state = _preflight(
        args,
        database,
        seed,
        namespace,
        source_root,
        args.archive_sha256,
        scratch,
        counters,
    )
    state["endpoint"] = route["tools"].get("endpoint")
    # The loopback endpoint is kept only in child memory; it is never part of report evidence.
    # The server used an ephemeral local port, so recover it from the already-bound listener.
    state["endpoint"] = f"http://127.0.0.1:{state['listener'].getsockname()[1]}/mcp"
    print(json.dumps(route, ensure_ascii=False, separators=(",", ":")), flush=True)
    instruction = sys.stdin.readline()
    try:
        command = json.loads(instruction) if instruction else {"action": "abort"}
    except json.JSONDecodeError:
        command = {"action": "abort"}
    if command.get("action") != "execute":
        state["manager"].close()
        state["server"].should_exit = True
        state["thread"].join(10)
        state["listener"].close()
        print(json.dumps({"aborted": True}), flush=True)
        return 0
    result = _execute(args, state, route, counters)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
