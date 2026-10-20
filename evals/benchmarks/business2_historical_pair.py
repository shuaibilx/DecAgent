"""Execution-only compatibility pair for the frozen business-2 source archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4, uuid5

ARCHIVE_RELATIVE = Path("generated/261015/0747_business2基线/business-2-source.zip")
ARCHIVE_SHA256 = "be2694b2b5a867daa7b0536fb12ce733a589ee74a1738aca2908608fa1d0745e"
HISTORICAL_SCHEMA_VERSION = 11
CURRENT_SCHEMA_VERSION = 17
SEED_RELATIVE = Path("evals/benchmarks/business2_pair_seed_v1.json")
CHILD_RELATIVE = Path("evals/benchmarks/_business2_pair_child.py")
PROTOCOL_VERSION = "2026-07-28"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_seed(path: Path | None = None) -> dict:
    seed_path = path or Path(__file__).resolve().with_name("business2_pair_seed_v1.json")
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if seed.get("schema_version") != "business2-semantic-seed-v1":
        raise ValueError("Unsupported business-2 semantic seed version")
    namespace = UUID(seed["namespace_uuid"])
    people = seed.get("people")
    categories = seed.get("categories")
    accounts = seed.get("accounts")
    if (
        not isinstance(people, list)
        or len(people) != 6
        or len({(row.get("name"), row.get("team")) for row in people}) != 6
        or not isinstance(categories, list)
        or len(categories) != 5
        or len(set(categories)) != 5
        or not isinstance(accounts, list)
        or len(accounts) != 6
    ):
        raise ValueError(
            "Semantic seed must contain six distinct people, five categories and six accounts"
        )
    if any(not row.get("name") or not row.get("team") for row in people):
        raise ValueError("Semantic seed has an incomplete person")
    logins = [row.get("login") for row in accounts]
    if len(set(logins)) != len(accounts) or any(not value for value in logins):
        raise ValueError("Semantic seed account logins must be non-empty and unique")
    if not seed_path.is_file():
        raise ValueError("Semantic seed file is unavailable")
    seed["seed_sha256"] = sha256(seed_path.read_bytes())
    seed["_namespace"] = namespace
    return seed


def resolve_case(seed: dict) -> dict:
    case = seed["case"]
    namespace = seed["_namespace"] if "_namespace" in seed else UUID(seed["namespace_uuid"])
    people = {(row["name"], row["team"]): row for row in seed["people"]}
    requester = case["requester"]
    if (requester["name"], requester["team"]) not in people:
        raise ValueError("Case requester is absent from the semantic seed")
    if case["category"] not in seed["categories"]:
        raise ValueError("Case category is absent from the semantic seed")
    expected_requester_id = str(uuid5(namespace, requester["name"] + ":" + requester["team"]))
    params = {
        "title": case["title"],
        "description": case["description"],
        "category_id": str(uuid5(namespace, "category:" + case["category"])),
        "priority": case["priority"],
    }
    instruction = json.dumps(
        {"command": case["command"], "params": params},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = {
        "case_id": case["case_id"],
        "command": case["command"],
        "params": params,
        "expected_requester_id": expected_requester_id,
        "instruction": instruction,
    }
    payload["case_digest"] = sha256(canonical(payload).encode("utf-8"))
    return payload


def _same(left: object, right: object) -> bool:
    return canonical(left) == canonical(right)


def evaluate_pair(historical: dict, current: dict, *, require_execution: bool = True) -> dict:
    """Mark a pair eligible only after actual state and governance parity is evidenced."""
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition and reason not in reasons:
            reasons.append(reason)

    routes = (historical, current)
    require(all(isinstance(route, dict) for route in routes), "route_evidence_missing")
    if not all(isinstance(route, dict) for route in routes):
        return {
            "status": "compatibility_only/ineligible",
            "performance_evidence_eligible": False,
            "scope": "execution_only",
            "reasons": reasons,
        }

    require(
        all(route.get("route") == "execution_only" for route in routes), "route_not_execution_only"
    )
    require(all(route.get("model_calls") == 0 for route in routes), "model_calls_nonzero")
    require(all(route.get("jev_calls") == 0 for route in routes), "jev_calls_nonzero")
    require(historical.get("fast_path") == "hybrid_literal", "historical_literal_fast_path_missing")
    require(
        current.get("fast_path") in {"literal_plan", "hybrid_literal"},
        "current_literal_route_missing",
    )
    require(
        historical.get("source", {}).get("archive_sha256") == ARCHIVE_SHA256,
        "historical_archive_hash_mismatch",
    )
    for route in routes:
        source = route.get("source", {})
        require(bool(source.get("provenance_digest")), "source_provenance_missing")
        require(bool(source.get("source_hashes")), "source_hashes_missing")
    for side, route in zip(("historical", "current"), routes, strict=True):
        source = route.get("source", {})
        source_payload = {key: value for key, value in source.items() if key != "provenance_digest"}
        source_digest = sha256(canonical(source_payload).encode("utf-8"))
        require(
            source.get("provenance_digest") == source_digest,
            f"{side}_source_provenance_digest_mismatch",
        )

    historical_db, current_db = (route.get("database", {}) for route in routes)
    require(
        historical_db.get("schema_version") == HISTORICAL_SCHEMA_VERSION,
        "historical_schema_mismatch",
    )
    require(current_db.get("schema_version") == CURRENT_SCHEMA_VERSION, "current_schema_mismatch")
    require(
        bool(historical_db.get("name")) and bool(current_db.get("name")),
        "database_identity_missing",
    )
    require(historical_db.get("name") != current_db.get("name"), "database_boundaries_not_distinct")
    require(
        historical_db.get("process_id") != current_db.get("process_id"),
        "process_boundaries_not_distinct",
    )

    expected_seed = load_seed()
    namespace = UUID(expected_seed["namespace_uuid"])
    expected_people = [
        {
            "id": str(uuid5(namespace, row["name"] + ":" + row["team"])),
            "name": row["name"],
            "team": row["team"],
            "active": True,
        }
        for row in expected_seed["people"]
    ]
    expected_people.sort(key=lambda row: (row["name"], row["team"], row["id"]))
    expected_categories = [
        {
            "id": str(uuid5(namespace, "category:" + name)),
            "name": name,
        }
        for name in expected_seed["categories"]
    ]
    expected_categories.sort(key=lambda row: (row["name"], row["id"]))
    expected_case = resolve_case(expected_seed)
    expected_case_payload = {
        "case_id": expected_case["case_id"],
        "command": expected_case["command"],
        "params": expected_case["params"],
        "expected_requester_id": expected_case["expected_requester_id"],
        "instruction": expected_case["instruction"],
    }
    expected_case_digest = sha256(
        canonical(
            {
                key: expected_case[key]
                for key in (
                    "case_id",
                    "command",
                    "params",
                    "expected_requester_id",
                    "instruction",
                )
            }
        ).encode("utf-8")
    )
    for side, route in zip(("historical", "current"), routes, strict=True):
        seed = route.get("seed", {})
        require(
            seed.get("version") == "business2-semantic-seed-v1", f"{side}_seed_version_mismatch"
        )
        require(
            seed.get("people_rows") == 6
            and isinstance(seed.get("people"), list)
            and len(seed["people"]) == 6,
            f"{side}_seed_not_nonempty",
        )
        require(
            seed.get("categories_rows") == 5
            and isinstance(seed.get("categories"), list)
            and len(seed["categories"]) == 5,
            f"{side}_seed_not_nonempty",
        )
        require(bool(seed.get("state_digest")), f"{side}_state_digest_missing")
        require(seed.get("verified_from_database") is True, f"{side}_database_seed_proof_missing")
        require(_same(seed.get("people"), expected_people), f"{side}_people_rows_mismatch")
        require(
            _same(seed.get("categories"), expected_categories), f"{side}_category_rows_mismatch"
        )
        state_digest = sha256(
            canonical({"people": seed.get("people"), "categories": seed.get("categories")}).encode(
                "utf-8"
            )
        )
        require(state_digest == seed.get("state_digest"), f"{side}_state_digest_mismatch")
        require(
            route.get("case_digest") == expected_case_digest,
            f"{side}_case_digest_mismatch",
        )
    require(
        _same(historical.get("seed", {}).get("people"), current.get("seed", {}).get("people"))
        and _same(
            historical.get("seed", {}).get("categories"),
            current.get("seed", {}).get("categories"),
        )
        and historical.get("seed", {}).get("state_digest")
        == current.get("seed", {}).get("state_digest"),
        "normalized_business_state_differs",
    )
    require(
        historical.get("case_digest") == current.get("case_digest")
        and bool(historical.get("case_digest"))
        and _same(historical.get("case"), expected_case_payload)
        and _same(current.get("case"), expected_case_payload),
        "case_differs",
    )

    for side, route in zip(("historical", "current"), routes, strict=True):
        identity = route.get("identity_permissions", {})
        require(bool(identity.get("actors")), f"{side}_identity_evidence_missing")
        require(bool(identity.get("probes")), f"{side}_permission_evidence_missing")
        require(bool(identity.get("digest")), f"{side}_identity_digest_missing")
        identity_payload = {key: value for key, value in identity.items() if key != "digest"}
        identity_digest = sha256(canonical(identity_payload).encode("utf-8"))
        require(
            identity.get("digest") == identity_digest,
            f"{side}_identity_permissions_digest_mismatch",
        )
    base_employee_commands = {
        "close",
        "comment",
        "create",
        "directory",
        "get",
        "list",
        "receipt",
        "reopen",
    }
    commands_by_role = {
        "employee": sorted(base_employee_commands),
        "handler": sorted(base_employee_commands | {"start", "resolve"}),
        "supervisor": sorted(
            base_employee_commands | {"assign", "start", "resolve", "set_priority"}
        ),
    }
    expected_role_commands = {
        account["login"]: commands_by_role[account["role"]]
        for account in expected_seed["accounts"]
    }
    accounts_by_login = {account["login"]: account for account in expected_seed["accounts"]}
    actor_fields = (
        "login",
        "employee_id",
        "person_id",
        "role",
        "source",
        "credential_ref_present",
        "allowed_commands",
    )
    expected_actors = {}
    for login in ("employee-it", "employee-facilities"):
        account = accounts_by_login[login]
        expected_actors[login] = {
            "login": login,
            "employee_id": str(uuid5(namespace, "account:" + login)),
            "person_id": str(uuid5(namespace, account["name"] + ":" + account["team"])),
            "role": account["role"],
            "source": "mcp_token",
            "credential_ref_present": True,
            "allowed_commands": expected_role_commands[login],
        }
    expected_probes = {
        "assign": "forbidden",
        "assign_advertised": False,
        "employee_it_list_before": 0,
        "employee_facilities_list_before": 0,
        "create": "authorized_by_role_policy",
    }
    if require_execution:
        expected_probes.update(
            {"employee_it_list_after": 1, "employee_facilities_list_after": 0}
        )
    for side, route in zip(("historical", "current"), routes, strict=True):
        identity = route.get("identity_permissions", {})
        actors = identity.get("actors")
        actor_map = {
            actor.get("login"): actor
            for actor in actors
            if isinstance(actor, dict) and isinstance(actor.get("login"), str)
        } if isinstance(actors, list) else {}
        actor_projection = {
            login: {field: actor_map[login].get(field) for field in actor_fields}
            for login in expected_actors
            if login in actor_map
        }
        require(
            isinstance(actors, list)
            and len(actors) == len(expected_actors)
            and _same(actor_projection, expected_actors),
            f"{side}_identity_actors_mismatch",
        )
        require(
            _same(identity.get("role_commands"), expected_role_commands),
            f"{side}_identity_role_permissions_mismatch",
        )
        probes = identity.get("probes", {})
        require(
            isinstance(probes, dict)
            and all(probes.get(key) == value for key, value in expected_probes.items()),
            f"{side}_identity_probe_mismatch",
        )
    require(
        _same(historical.get("identity_permissions"), current.get("identity_permissions")),
        "identity_permissions_differ",
    )

    for side, route in zip(("historical", "current"), routes, strict=True):
        tools = route.get("tools", {})
        require(tools.get("handshake_completed") is True, f"{side}_mcp_handshake_missing")
        require(tools.get("protocol_version") == PROTOCOL_VERSION, f"{side}_mcp_protocol_mismatch")
        require(bool(tools.get("schemas")), f"{side}_full_tool_schemas_missing")
        require(bool(tools.get("schema_digest")), f"{side}_tool_schema_digest_missing")
        schema_digest = sha256(canonical(tools.get("schemas")).encode("utf-8"))
        require(
            tools.get("schema_digest") == schema_digest,
            f"{side}_tool_schema_digest_mismatch",
        )
    require(
        _same(historical.get("tools", {}).get("schemas"), current.get("tools", {}).get("schemas"))
        and historical.get("tools", {}).get("schema_digest")
        == current.get("tools", {}).get("schema_digest"),
        "tool_schemas_differ",
    )

    for side, route in zip(("historical", "current"), routes, strict=True):
        governance = route.get("governance", {})
        assessment = governance.get("assessment", {})
        require(
            all(key in assessment for key in ("outcome", "reason", "policy_version", "resource")),
            f"{side}_governance_evidence_missing",
        )
        require(
            assessment.get("outcome") == "allow",
            f"{side}_governance_outcome_not_allow",
        )
        require(
            assessment.get("resource") is None,
            f"{side}_governance_resource_mismatch",
        )
        require(
            isinstance(assessment.get("reason"), str) and bool(assessment["reason"].strip()),
            f"{side}_governance_reason_missing",
        )
        require(
            isinstance(assessment.get("policy_version"), str)
            and bool(assessment["policy_version"].strip()),
            f"{side}_governance_policy_version_missing",
        )
        require(
            isinstance(governance.get("effective_params"), dict), f"{side}_effective_params_missing"
        )
        expected_effective_params = {
            **expected_case["params"],
            "requester_id": expected_case["expected_requester_id"],
        }
        require(
            _same(governance.get("effective_params"), expected_effective_params),
            f"{side}_effective_params_case_mismatch",
        )
        digest_field = "digest" if require_execution else "preflight_digest"
        digest_reason = (
            f"{side}_governance_digest_mismatch"
            if require_execution
            else f"{side}_governance_preflight_digest_mismatch"
        )
        governance_digest_payload = {
            "assessment": governance.get("assessment"),
            "effective_params": governance.get("effective_params"),
        }
        governance_digest = sha256(canonical(governance_digest_payload).encode("utf-8"))
        require(bool(governance.get(digest_field)), f"{side}_governance_{digest_field}_missing")
        require(governance.get(digest_field) == governance_digest, digest_reason)

    def governance_projection(route):
        governance = route.get("governance", {})
        return {
            "assessment": governance.get("assessment"),
            "effective_params": governance.get("effective_params"),
        }

    require(
        _same(governance_projection(historical), governance_projection(current)),
        "governance_evidence_differ",
    )

    if require_execution:
        ticket_business_fields = (
            "title",
            "description",
            "category_id",
            "priority",
            "requester_id",
            "employee_id",
            "status",
        )
        expected_ticket_projection = {
            **expected_case["params"],
            "requester_id": expected_case["expected_requester_id"],
            "employee_id": str(uuid5(namespace, "account:employee-it")),
            "status": "open",
        }
        ticket_projections = []
        for side, route in zip(("historical", "current"), routes, strict=True):
            require(
                route.get("governance", {}).get("persisted_action") is True,
                f"{side}_persisted_action_missing",
            )
            execution = route.get("execution", {})
            require(
                execution.get("task_state") == "succeeded",
                f"{side}_task_state_not_succeeded",
            )
            require(execution.get("ticket_rows") == 1, f"{side}_ticket_rows_invalid")
            require(
                execution.get("business_row_counts")
                == {"tickets": 1, "ticket_history": 1, "operation_receipts": 2},
                f"{side}_business_row_counts_invalid",
            )
            require(
                execution.get("write_action_count") == 1,
                f"{side}_write_action_count_invalid",
            )
            require(
                execution.get("requester_matches_seed") is True,
                f"{side}_requester_seed_mismatch",
            )
            ticket = execution.get("ticket")
            require(isinstance(ticket, dict), f"{side}_ticket_missing")
            ticket_projection = (
                {key: ticket.get(key) for key in ticket_business_fields}
                if isinstance(ticket, dict)
                else None
            )
            if ticket_projection is not None:
                ticket_projections.append(ticket_projection)
                require(
                    _same(ticket_projection, expected_ticket_projection),
                    f"{side}_ticket_business_fields_mismatch",
                )
            require(
                isinstance(ticket, dict)
                and str(ticket.get("requester_id")) == expected_case["expected_requester_id"],
                f"{side}_ticket_requester_mismatch",
            )
            require(
                execution.get("employee_it_visible_tickets") == 1,
                f"{side}_employee_it_visibility_invalid",
            )
            require(
                execution.get("employee_facilities_visible_tickets") == 0,
                f"{side}_employee_facilities_visibility_invalid",
            )
        require(
            len(ticket_projections) == 2
            and _same(ticket_projections[0], ticket_projections[1]),
            "ticket_business_projections_differ",
        )
        require(
            all(route.get("execution", {}).get("status") == "succeeded" for route in routes),
            "execution_not_successful",
        )
    else:
        require(
            all(route.get("execution", {}).get("status") == "pending" for route in routes),
            "preflight_execution_state_invalid",
        )

    if reasons:
        return {
            "status": "compatibility_only/ineligible",
            "performance_evidence_eligible": False,
            "scope": "execution_only",
            "reasons": sorted(reasons),
        }
    return {
        "status": "execution_only_eligible" if require_execution else "preflight_eligible",
        "performance_evidence_eligible": require_execution,
        "scope": "execution_only",
        "reasons": [],
    }


def _check_archive(path: Path) -> None:
    if not path.is_file() or sha256(path.read_bytes()) != ARCHIVE_SHA256:
        raise ValueError("Frozen business-2 archive is absent or has changed")


def _extract_archive(archive: Path, destination: Path) -> Path:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (root / member.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Frozen archive contains a path outside its extraction directory")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(member))
    return root


def _child_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "VIRTUAL_ENV"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


@contextmanager
def _temporary_database(admin_url: str, prefix: str):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = prefix + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin_url, dbname=name), name
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _child_command(
    side: str, source_root: Path, seed_path: Path, archive_hash: str, cwd: Path
) -> list[str]:
    child = Path(__file__).resolve().with_name("_business2_pair_child.py")
    return [
        sys.executable,
        "-I",
        "-B",
        str(child),
        "--side",
        side,
        "--source-root",
        str(source_root),
        "--seed-path",
        str(seed_path),
        "--scratch-dir",
        str(cwd / ("child-" + side)),
        "--archive-sha256",
        archive_hash,
    ]


def _send_database_bootstrap(process, database_url: str) -> None:
    if process.stdin is None:
        raise RuntimeError("Business-2 pair subprocess has no bootstrap pipe")
    process.stdin.write(json.dumps({"database_url": database_url}, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _start_child(
    side: str, source_root: Path, db_url: str, seed_path: Path, archive_hash: str, cwd: Path
):
    command = _child_command(side, source_root, seed_path, archive_hash, cwd)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    readers = []
    try:
        _send_database_bootstrap(process, db_url)
        lines: queue.Queue[tuple[str, str]] = queue.Queue()
        errors: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(("line", line))
            lines.put(("eof", ""))

        def read_stderr() -> None:
            assert process.stderr is not None
            errors.extend(process.stderr.readlines())

        stdout_reader = threading.Thread(target=read_stdout, daemon=True)
        stdout_reader.start()
        readers.append(stdout_reader)
        stderr_reader = threading.Thread(target=read_stderr, daemon=True)
        stderr_reader.start()
        readers.append(stderr_reader)
        return process, lines, errors
    except BaseException:
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=10)
            except OSError:
                pass
        finally:
            for reader in readers:
                try:
                    reader.join(timeout=10)
                except RuntimeError:
                    pass
        raise


def _receive(process, lines, errors, timeout: float = 120) -> dict:
    try:
        kind, line = lines.get(timeout=timeout)
    except queue.Empty as exc:
        process.kill()
        raise TimeoutError("Business-2 pair subprocess timed out") from exc
    if kind != "line":
        process.wait(timeout=10)
        detail = "".join(errors).strip()
        raise RuntimeError("Business-2 pair subprocess exited without evidence: " + detail[-4000:])
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Business-2 pair subprocess returned invalid JSON evidence") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Business-2 pair subprocess evidence must be a JSON object")
    return value


def _finish(process, errors) -> None:
    code = process.wait(timeout=120)
    if code:
        raise RuntimeError("Business-2 pair subprocess failed: " + "".join(errors).strip()[-4000:])


def _cleanup_children(children) -> None:
    processes = [child[0] for child in children]
    for process in processes:
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            # A child may exit between poll() and kill(); still wait for it below.
            pass
    cleanup_errors = []
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=10)
            except Exception as error:  # Keep cleaning the remaining children.
                cleanup_errors.append(error)
        except Exception as error:  # Keep cleaning the remaining children.
            cleanup_errors.append(error)
    if cleanup_errors:
        raise RuntimeError("Could not confirm all business-2 pair children stopped") from (
            cleanup_errors[0]
        )


@contextmanager
def _started_children(child_specs):
    children = []
    try:
        for args in child_specs:
            children.append(_start_child(*args))
        yield children
    finally:
        _cleanup_children(children)


def run_pair(admin_url: str, *, archive_path: Path | None = None) -> dict:
    """Run preflight in both isolated source processes, then execute only if parity holds."""
    root = Path(__file__).resolve().parents[2]
    archive = archive_path or root / ARCHIVE_RELATIVE
    _check_archive(archive)
    seed_path = root / SEED_RELATIVE
    seed = load_seed(seed_path)
    seed_sha = seed["seed_sha256"]
    source_hash = ARCHIVE_SHA256
    current_source_root = root
    with tempfile.TemporaryDirectory(prefix="decagent-business2-pair-") as scratch:
        scratch_path = Path(scratch)
        historical_root = _extract_archive(archive, scratch_path / "business-2-source")
        with (
            _temporary_database(admin_url, "b2hist_") as (historical_url, historical_db_name),
            _temporary_database(admin_url, "b2curr_") as (current_url, current_db_name),
        ):
            child_specs = [
                (
                    "historical",
                    historical_root,
                    historical_url,
                    seed_path,
                    source_hash,
                    scratch_path,
                ),
                (
                    "current",
                    current_source_root,
                    current_url,
                    seed_path,
                    source_hash,
                    scratch_path,
                ),
            ]
            with _started_children(child_specs) as children:
                historical_process, historical_lines, historical_errors = children[0]
                current_process, current_lines, current_errors = children[1]
                historical_preflight = _receive(
                    historical_process, historical_lines, historical_errors
                )
                current_preflight = _receive(current_process, current_lines, current_errors)
                preflight_gate = evaluate_pair(
                    historical_preflight, current_preflight, require_execution=False
                )
                proceed = preflight_gate["status"] == "preflight_eligible"
                answer = json.dumps({"action": "execute" if proceed else "abort"}) + "\n"
                for process, _, _ in children:
                    assert process.stdin is not None
                    process.stdin.write(answer)
                    process.stdin.flush()
                if not proceed:
                    _finish(historical_process, historical_errors)
                    _finish(current_process, current_errors)
                    return {
                        "schema_version": "business2-execution-only-pair-v1",
                        "route": "execution_only",
                        "status": preflight_gate["status"],
                        "performance_evidence_eligible": False,
                        "preflight": preflight_gate,
                        "historical": historical_preflight,
                        "current": current_preflight,
                    }
                historical_final = _receive(historical_process, historical_lines, historical_errors)
                current_final = _receive(current_process, current_lines, current_errors)
                _finish(historical_process, historical_errors)
                _finish(current_process, current_errors)

    historical_final["database"]["name"] = historical_db_name
    current_final["database"]["name"] = current_db_name
    gate = evaluate_pair(historical_final, current_final)
    return {
        "schema_version": "business2-execution-only-pair-v1",
        "route": "execution_only",
        "status": gate["status"],
        "performance_evidence_eligible": gate["performance_evidence_eligible"],
        "eligibility": gate,
        "seed": {"version": seed["schema_version"], "sha256": seed_sha},
        "historical": historical_final,
        "current": current_final,
        "governance_source_hashes_equal": None,
        "notes": [
            "This pair covers deterministic execution only and makes no model-quality claim.",
            "Governance hashes are reported per source file; equality is not assumed.",
            "A compatibility_only/ineligible pair is not performance evidence.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DECAGENT_TEST_DATABASE_URL"),
        help="admin DSN for a dedicated local disposable PostgreSQL cluster",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DECAGENT_TEST_DATABASE_URL is required")
    report = run_pair(args.database_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return 0 if report["status"] == "execution_only_eligible" else 2


if __name__ == "__main__":
    raise SystemExit(main())
