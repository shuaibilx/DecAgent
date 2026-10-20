"""Verify the pinned tau2 task runtime offline; never score or call a model.

Run with the isolated upstream Python environment, not DecAgent's application
environment. The upstream lock plus websockets==15.0.1 on Python 3.12 is needed
because this pinned revision eagerly imports an optional voice module.
"""

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import socket
import sys
import tarfile
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_archive(root, source, record):
    archive = root / record["archive"]["path"]
    if sha256(archive.read_bytes()).hexdigest() != record["archive"]["sha256"]:
        raise ValueError("Pinned tau2 source archive checksum differs")
    checked = 0
    with tarfile.open(archive) as package:
        for member in package:
            if not member.isfile():
                continue
            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
            name = str(relative)
            selected = (
                (name.startswith("src/tau2/") and name.endswith(".py"))
                or name in {"pyproject.toml", "uv.lock"}
                or name.startswith(("data/tau2/domains/retail/", "data/tau2/domains/telecom/"))
            )
            if not selected:
                continue
            target = (source / name).resolve()
            if not target.is_relative_to(source.resolve()):
                raise ValueError("Pinned tau2 source path escapes its source tree")
            stream = package.extractfile(member)
            if (
                stream is None
                or sha256(stream.read()).digest() != sha256(target.read_bytes()).digest()
            ):
                raise ValueError("Installed tau2 source or domain data differs from archive")
            checked += 1
    if checked < 10:
        raise ValueError("Pinned tau2 source verification was incomplete")
    return checked


def initialize(factory, task):
    environment = factory()
    initial = deepcopy(task.initial_state)
    environment.set_state(
        initialization_data=initial.initialization_data if initial else None,
        initialization_actions=initial.initialization_actions if initial else None,
        message_history=(initial.message_history or []) if initial else [],
    )
    return environment


def verify(root):
    root = Path(root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from decagent.evaluation.business.manifest_hashing import task_inventory_sha256

    manifest = json.loads((root / "evals/benchmarks/manifests/source_manifest.json").read_text())
    source_record = manifest["tau2_bench"]["source_repository"]
    source = (root / source_record["local_path"]).resolve()
    checked = verify_archive(root, source, source_record)
    inventory_path = root / manifest["task_inventory"]["path"]
    if task_inventory_sha256(inventory_path.read_bytes()) != manifest["task_inventory"]["sha256"]:
        raise ValueError("Pinned task inventory checksum differs")
    inventory = json.loads(inventory_path.read_text())["resource_subsets"]["tau2_bench_supplement"][
        "domains"
    ]
    os.environ.update(
        TAU2_DATA_DIR=str(source / "data"),
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        LOGURU_LEVEL="ERROR",
    )
    network_attempts = []

    def deny_network(*_args, **_kwargs):
        network_attempts.append(True)
        raise RuntimeError("Network access is forbidden during offline tau2 verification")

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    cases = []
    try:
        package = importlib.import_module("tau2")
        if not Path(package.__file__).resolve().is_relative_to(source / "src"):
            raise ValueError("Imported tau2 is not the pinned source tree")
        if importlib.metadata.version("tau2") != source_record["package_version"]:
            raise ValueError("Installed tau2 package version differs")
        for domain in source_record["selected_domains"]:
            original = importlib.import_module("tau2.domains." + domain + ".environment")
            expected = inventory["tau2-bench/" + domain]
            tasks = original.get_tasks(expected["official_split"])
            if {task.id for task in tasks} != set(expected["task_ids"]):
                raise ValueError("Upstream official task IDs differ from frozen inventory")
            for task in sorted(tasks, key=lambda value: value.id):
                row = {"domain": domain, "case_id": task.id}
                try:
                    environment = initialize(original.get_environment, task)
                    reset = initialize(original.get_environment, task)
                    state = [environment.get_db_hash(), environment.get_user_db_hash()]
                    if state != [reset.get_db_hash(), reset.get_user_db_hash()]:
                        raise ValueError("Fresh upstream initial states differ")
                    tools = environment.get_tools()
                    row.update(
                        status="ready",
                        initial_state_digest=digest(state),
                        tool_catalog_digest=digest([tool.openai_schema for tool in tools]),
                        tool_count=len(tools),
                        mutating_tool_count=sum(
                            environment.tools.tool_mutates_state(tool.name) for tool in tools
                        ),
                    )
                except Exception as exc:
                    row.update(status="failed", error_type=type(exc).__name__)
                cases.append(row)
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_connect_ex
    failed = [row for row in cases if row["status"] != "ready"]
    return {
        "schema_version": "tau2-offline-runtime-readiness-1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "ready" if not failed and not network_attempts else "not_ready",
        "source_commit": source_record["commit"],
        "source_archive_sha256": source_record["archive"]["sha256"],
        "verified_source_and_domain_files": checked,
        "upstream_lock_sha256": sha256((source / "uv.lock").read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "package_version": importlib.metadata.version("tau2"),
        "runtime_additions": {"websockets": importlib.metadata.version("websockets")},
        "initialized_cases": len(cases),
        "ready_cases": len(cases) - len(failed),
        "failed_cases": len(failed),
        "network_attempts_blocked": len(network_attempts),
        "model_requests": 0,
        "formal_task_attempts": 0,
        "task_success_rate": None,
        "scope": "Official test task loading, initialization and fresh-state equality only",
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Choose a new runtime evidence output path")
    root = Path(__file__).resolve().parents[2]
    report = verify(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "initialized_cases",
                    "ready_cases",
                    "failed_cases",
                    "model_requests",
                )
            }
        )
    )
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
