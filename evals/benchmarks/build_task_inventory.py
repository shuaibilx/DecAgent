"""Build a task-ID-only inventory from pinned local benchmark sources.

The output contains task IDs, official splits, configured tool names and
heuristic task-family facets. It omits prompts, tool results, verifiers, database
state, and reference trajectories so the inventory cannot become agent input.
Install the optional reader with ``python -m pip install -r
evals/benchmarks/requirements.txt`` before rebuilding the inventory.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "evals" / "benchmarks" / "vendor_cache"
ENTERPRISE_DATASET = VENDOR / "enterpriseops-gym-dataset"
TAU2_DATA = VENDOR / "tau2-bench-source" / "data" / "tau2" / "domains"
CONFIGS = ("oracle", "plus_5_tools", "plus_10_tools", "plus_15_tools")
ENTERPRISE_DOMAINS = ("csm", "itsm")
GOLD_FIELDS = {
    "system_prompt",
    "user_prompt",
    "user_scenario",
    "initial_state",
    "evaluation_criteria",
    "verifiers",
    "reference",
    "reference_actions",
    "expected_actions",
    "expected_state",
    "target_state",
    "tool_results",
    "database_contents",
    "labels",
    "answer",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _annotation(text: str, domain: str, tools: list[str]) -> dict[str, Any]:
    """Emit explicit surface-signal labels; do not infer gold trajectories."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    family_rules = (
        ("account_contract_onboarding", r"account|contract|entitlement|customer onboarding"),
        ("case_ticket_lifecycle", r"case|ticket|incident|service request"),
        ("asset_product_lifecycle", r"product|device|install|asset|serial number"),
        ("retail_order_after_sales", r"order|shipment|return|refund|exchange|delivery"),
        ("telecom_account_service", r"plan|phone line|device|bill|data usage|service"),
    )
    family = next(
        (name for name, pattern in family_rules if re.search(pattern, normalized)),
        f"{domain}_other",
    )
    signals = {
        "multi_step": (
            r"\b(then|after|before|once|next|also|and then)\b"
            r"|;|\bwhile\b"
        ),
        "state_dependent": (
            r"\b(existing|current|previous|same|again|already|last|prior|once|after)\b"
            r"|#[a-z0-9-]+"
        ),
        "structured_branch": (
            r"\b(if|when|unless|whether|status|priority|sla|overdue|within|"
            r"greater than|less than)\b"
        ),
        "runtime_semantic_branch": (
            r"\b(which|choose|select|match|appropriate|similar|ambiguous|"
            r"category|assign to|belongs to)\b"
        ),
        "dialogue_clarification": (
            r"\b(ask me|clarify|unclear|not sure|which one|more information|"
            r"missing information|confirm with me)\b"
        ),
        "exception_recovery": (
            r"\b(error|failed|failure|retry|cancel|refund|wrong|incorrect|"
            r"duplicate|again|reopen|unable|not working)\b"
        ),
    }
    found = {
        name: sorted(set(re.findall(pattern, normalized))) for name, pattern in signals.items()
    }
    return {
        "workflow_family": family,
        "workflow_depth_estimate": "explicit_multi_step" if found["multi_step"] else "not_explicit",
        "available_tool_names": sorted(set(tools)),
        "tool_dependency_scope": "task_configured_toolset; exact required trajectory not inferred",
        "state_dependent_candidate": bool(found["state_dependent"]),
        "structured_branch_candidate": bool(found["structured_branch"]),
        "runtime_semantic_branch_candidate": bool(found["runtime_semantic_branch"]),
        "dialogue_clarification_candidate": bool(found["dialogue_clarification"]),
        "exception_recovery_candidate": bool(found["exception_recovery"]),
        "surface_signals": found,
        "annotation_method": "prompt-surface-heuristics-1",
        "review_status": "heuristic_unreviewed",
    }


def _tau_tool_names(domain: str) -> list[str]:
    path = VENDOR / "tau2-bench-source" / "src" / "tau2" / "domains" / domain / "tools.py"
    if not path.is_file():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith(
            "_"
        ):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "is_tool"
            for decorator in node.decorator_list
        ):
            names.append(node.name)
    return sorted(set(names))


def _tau_user_instructions(scenario: Any) -> str:
    if isinstance(scenario, dict):
        instructions = scenario.get("instructions", "")
        if isinstance(instructions, dict):
            return " ".join(str(value) for value in instructions.values() if value is not None)
        return str(instructions)
    return str(scenario or "")


def _read_enterpriseops() -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - optional benchmark dependency
        raise SystemExit(
            "Task inventory generation needs pyarrow; install "
            "evals/benchmarks/requirements.txt first."
        ) from exc

    modes: list[dict[str, Any]] = []
    for config in CONFIGS:
        for domain in ENTERPRISE_DOMAINS:
            path = ENTERPRISE_DATASET / config / f"{domain}-00000-of-00001.parquet"
            if not path.is_file():
                raise FileNotFoundError(f"Pinned EnterpriseOps split is missing: {path}")

            rows = parquet.read_table(
                path,
                columns=["task_id", "gym_servers_config", "user_prompt", "selected_tools"],
            ).to_pylist()
            task_ids = [str(row["task_id"]) for row in rows]
            if len(task_ids) != len(set(task_ids)):
                raise ValueError(f"Duplicate task IDs in {config}/{domain}")

            database_files: set[str] = set()
            task_annotations = []
            for row in rows:
                task_annotations.append(
                    {
                        "task_id": str(row["task_id"]),
                        **_annotation(
                            str(row["user_prompt"] or ""),
                            domain,
                            list(row["selected_tools"] or []),
                        ),
                    }
                )
                config_value = row["gym_servers_config"]
                servers = (
                    json.loads(config_value) if isinstance(config_value, str) else config_value
                )
                if not isinstance(servers, list):
                    raise ValueError(f"Invalid gym_servers_config in {config}/{domain}")
                for server in servers:
                    database_file = server.get("seed_database_file")
                    if database_file:
                        database_files.add(str(database_file).replace("\\", "/"))

            modes.append(
                {
                    "config": config,
                    "domain": domain,
                    "row_count": len(rows),
                    "execution_status": "not_run",
                    "task_ids": task_ids,
                    "task_annotations": task_annotations,
                    "seed_database_files": sorted(database_files),
                    "parquet_sha256": _sha256(path),
                }
            )
    return modes


def _read_tau2() -> list[dict[str, Any]]:
    domains: list[dict[str, Any]] = []
    for domain_dir in sorted(path for path in TAU2_DATA.iterdir() if path.is_dir()):
        tasks_path = domain_dir / "tasks.json"
        if not tasks_path.is_file():
            continue

        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
        if isinstance(tasks, dict):
            tasks = tasks.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError(f"Invalid task list: {tasks_path}")

        task_ids = [str(task["id"]) for task in tasks if isinstance(task, dict) and "id" in task]
        tool_names = _tau_tool_names(domain_dir.name)
        task_annotations = [
            {
                "task_id": str(task["id"]),
                **_annotation(
                    _tau_user_instructions(task.get("user_scenario")),
                    domain_dir.name,
                    tool_names,
                ),
            }
            for task in tasks
            if isinstance(task, dict) and "id" in task
        ]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError(f"Duplicate task IDs in τ-Bench domain {domain_dir.name}")

        split_path = domain_dir / "split_tasks.json"
        splits = json.loads(split_path.read_text(encoding="utf-8")) if split_path.is_file() else {}
        if not isinstance(splits, dict):
            raise ValueError(f"Invalid task splits: {split_path}")
        split_ids: dict[str, list[str]] = {}
        available = set(task_ids)
        for split_name, raw_ids in sorted(splits.items()):
            if not isinstance(raw_ids, list):
                continue
            normalized_ids = [str(task_id) for task_id in raw_ids]
            missing = sorted(set(normalized_ids) - available)
            if missing:
                raise ValueError(f"Split {domain_dir.name}/{split_name} references absent task IDs")
            split_ids[str(split_name)] = normalized_ids

        source_hashes = {
            name: _sha256(domain_dir / name)
            for name in (
                "tasks.json",
                "split_tasks.json",
                "policy.md",
                "db.json",
                "db.toml",
                "user_db.json",
                "user_db.toml",
            )
            if (domain_dir / name).is_file()
        }
        domains.append(
            {
                "domain": domain_dir.name,
                "task_count": len(task_ids),
                "execution_status": "not_run",
                "task_ids": task_ids,
                "task_annotations": task_annotations,
                "splits": split_ids,
                "source_sha256": source_hashes,
            }
        )
    return domains


def _stable_subset(task_ids: list[str], *, limit: int, seed: str) -> list[str]:
    if limit < 1:
        raise ValueError("subset limit must be positive")
    return sorted(
        task_ids,
        key=lambda task_id: hashlib.sha256(f"{seed}\0{task_id}".encode()).hexdigest(),
    )[:limit]


def _validate_agent_boundary(inventory: dict[str, Any]) -> None:
    """Reject scorer-only columns from the inventory used for task selection."""
    stack = [inventory]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in GOLD_FIELDS:
                    raise ValueError(f"Task inventory contains gold/scorer field: {key}")
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)

    boundary = inventory.get("agent_input_boundary")
    required = {
        "user and simulator prompts",
        "verifier sql and expected state",
        "reference actions",
        "tool results",
        "database contents",
    }
    declared = (
        {str(item).casefold() for item in boundary.get("excluded_from_inventory", [])}
        if isinstance(boundary, dict)
        else set()
    )
    if not required <= declared:
        raise ValueError("Task inventory must document every excluded gold/scorer field")


def build_inventory() -> dict[str, Any]:
    enterprise_modes = _read_enterpriseops()
    tau_domains = _read_tau2()
    enterprise_primary = [mode for mode in enterprise_modes if mode["config"] == "oracle"]
    if sum(mode["row_count"] for mode in enterprise_primary) != 206:
        raise ValueError("Pinned CSM/ITSM oracle row counts changed; review the manifest")

    tau_by_domain = {domain["domain"]: domain for domain in tau_domains}
    selected_tau = []
    for domain_name in ("retail", "telecom"):
        domain = tau_by_domain.get(domain_name)
        if domain is None or "test" not in domain["splits"]:
            raise ValueError(f"Required τ-Bench test split is missing: {domain_name}")
        selected_tau.append(domain)

    smoke_subsets = {
        f"enterpriseops/{mode['domain']}": {
            "seed": "decagent-benchmark-smoke-v1",
            "task_ids": _stable_subset(
                mode["task_ids"],
                limit=min(10, mode["row_count"]),
                seed="decagent-benchmark-smoke-v1",
            ),
        }
        for mode in enterprise_primary
    }
    tau_test_subsets = {
        f"tau2-bench/{domain['domain']}": {
            "official_split": "test",
            "task_ids": domain["splits"]["test"],
        }
        for domain in selected_tau
    }

    inventory = {
        "schema_version": 2,
        "sources": {
            "enterpriseops_dataset_revision": "c8e538eae8a6205294f0a86675fefdc1fac408f6",
            "tau2_bench_commit": "96a3ef92329baf908ffe9dfe188cea34e8701d2a",
        },
        "enterpriseops_gym": {
            "official_modes": enterprise_modes,
            "primary_selection": {
                "mode": "oracle",
                "domains": ["csm", "itsm"],
                "row_count": sum(mode["row_count"] for mode in enterprise_primary),
                "task_ids": {mode["domain"]: mode["task_ids"] for mode in enterprise_primary},
            },
        },
        "tau2_bench": {
            "official_domains": tau_domains,
            "resource_limited_selection": {
                "split": "test",
                "execution_status": "not_run",
                "domains": ["retail", "telecom"],
                "task_ids": {domain["domain"]: domain["splits"]["test"] for domain in selected_tau},
            },
        },
        "resource_subsets": {
            "enterpriseops_smoke": {
                "selection_rule": (
                    "lowest SHA-256(seed + NUL + task_id), at most 10 per selected domain"
                ),
                "execution_status": "not_run",
                "domains": smoke_subsets,
            },
            "tau2_bench_supplement": {
                "selection_rule": "all task IDs in the official test split for retail and telecom",
                "execution_status": "not_run",
                "domains": tau_test_subsets,
            },
        },
        "agent_input_boundary": {
            "inventory_contains": [
                "task IDs",
                "domain and tool-set config labels",
                "official split membership",
                "seed database file references",
                "source file hashes",
                "resource subset IDs",
            ],
            "excluded_from_inventory": [
                "user and simulator prompts",
                "verifier SQL and expected state",
                "reference actions",
                "tool results",
                "database contents",
            ],
        },
    }
    _validate_agent_boundary(inventory)
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evals" / "benchmarks" / "manifests" / "task_inventory.json",
    )
    args = parser.parse_args()
    inventory = build_inventory()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "enterpriseops_oracle_tasks": inventory["enterpriseops_gym"]["primary_selection"][
                    "row_count"
                ],
                "tau2_test_tasks": sum(
                    len(ids)
                    for ids in inventory["tau2_bench"]["resource_limited_selection"][
                        "task_ids"
                    ].values()
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
