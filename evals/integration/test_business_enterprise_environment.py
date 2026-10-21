"""Opt-in local Docker readiness; never a task score and never a model call.

Run with DECAGENT_ENTERPRISEOPS_DOCKER_SMOKE=1. Creates and removes only labelled
trial containers, using the first frozen smoke ID in each selected domain.
"""

import asyncio
import importlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from decagent.evaluation.business.enterprise_environment import (
    EnterpriseEnvironment,
    spec_from_pinned_row,
)
from decagent.evaluation.business.enterprise_scoring import EnterpriseOfficialScorer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("DECAGENT_ENTERPRISEOPS_DOCKER_SMOKE") != "1",
        reason="Opt-in local pinned Docker environment readiness only",
    ),
]
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "domain,read_tool",
    [("csm", "retrieve_knowledge"), ("itsm", "list_users")],
)
def test_pinned_container_snapshot_mcp_and_original_sql_verifier(domain, read_tool, monkeypatch):
    parquet = pytest.importorskip("pyarrow.parquet")
    manifest = json.loads((ROOT / "evals/benchmarks/manifests/source_manifest.json").read_text())
    inventory = json.loads((ROOT / "evals/benchmarks/manifests/task_inventory.json").read_text())
    task_id = inventory["resource_subsets"]["enterpriseops_smoke"]["domains"][
        f"enterpriseops/{domain}"
    ]["task_ids"][0]
    source = manifest["enterpriseops_gym"]
    data_record = next(
        record
        for record in source["task_dataset"]["parquet_sha256"]
        if record["path"].endswith(f"oracle/{domain}-00000-of-00001.parquet")
    )
    data_path = ROOT / data_record["path"]
    assert sha256(data_path.read_bytes()).hexdigest() == data_record["sha256"]
    row = next(
        row for row in parquet.read_table(data_path).to_pylist() if row["task_id"] == task_id
    )
    spec = spec_from_pinned_row(ROOT, row, manifest)

    # Import the actual pinned engine, not a local implementation of its rules.
    upstream = ROOT / "evals/benchmarks/vendor_cache/enterpriseops-gym"
    monkeypatch.syspath_prepend(str(upstream))
    original = importlib.import_module("benchmark.verifier")
    models = importlib.import_module("benchmark.models")
    # A connectivity assertion only. No task's hidden verifier or expected state
    # is executed, and the original SQL extraction/comparison semantics are used.
    readiness = models.VerifierConfig(
        verifier_type="database_state",
        gym_name=spec.server_name,
        validation_config={"query": "SELECT 1 AS readiness", "expected_value": 1},
    )
    previous_database = None
    previous_container = None
    previous_read = None
    previous_state_fingerprint = None
    verifier_runs = []
    for reset_index in range(2):
        with EnterpriseEnvironment(spec) as trial:
            assert trial.database_id != previous_database
            assert trial._container.container_id != previous_container
            state_fingerprint = trial.state_fingerprint()
            assert len(state_fingerprint["digest"]) == 64
            assert state_fingerprint["table_count"] > 0
            assert state_fingerprint["row_count"] > 0
            if previous_state_fingerprint is not None:
                assert state_fingerprint == previous_state_fingerprint
            public = trial.model_input()
            assert public["system_prompt"] == row["system_prompt"]
            assert public["user_prompt"] == row["user_prompt"]
            assert set(public) == {"system_prompt", "user_prompt", "tools"}
            assert "verifiers" not in repr(public)
            names = {tool["name"] for tool in public["tools"]}
            assert names == set(row["selected_tools"]) - set(row["restricted_tools"] or [])
            read = trial.call_tool(read_tool, {})
            assert read.get("isError", False) is False
            assert read.get("content")
            if previous_read is not None:
                assert read == previous_read
            hook = trial.scorer_hook()
            engine = original.VerifierEngine({spec.server_name: hook.client}, llm_client=None)
            result = asyncio.run(hook.run(engine, [readiness], {}))
            assert result[0]["passed"] is True
            assert result[0]["actual"] == 1
            # Exercise the new private scorer/session boundary with synthetic
            # checks only; this never executes this task's hidden verifier.
            scorer = EnterpriseOfficialScorer(
                replace(
                    spec,
                    verifiers=(
                        {
                            "verifier_type": "database_state",
                            "gym_name": spec.server_name,
                            "validation_config": {
                                "query": "SELECT 1 AS readiness",
                                "expected_value": 1,
                            },
                        },
                        {
                            "verifier_type": "tool_execution",
                            "gym_name": spec.server_name,
                            "validation_config": {
                                "selected_tools": [read_tool],
                                "minimum_tool_calls": 1,
                            },
                        },
                    ),
                )
            )
            score = scorer.score_with_model_response(
                trial,
                {"content": "", "tool_calls": [{"name": read_tool, "args": {}}]},
                initial_state_fingerprint=state_fingerprint,
            )
            assert score.success and score.forbidden_side_effects is None
            assert score.judge_requests == 0
            assert scorer.private_audit_evidence["post_state_fingerprint"] == state_fingerprint

            # Run the frozen task's actual private verifier against the fresh,
            # unchanged database. With no task actions, this is an official
            # task failure, not an environment or scorer failure. The verifier
            # configuration and result rows remain evaluator-only.
            task_scorer = EnterpriseOfficialScorer(spec)
            task_score = task_scorer.score_with_model_response(
                trial,
                {"content": "", "tool_calls": []},
                initial_state_fingerprint=state_fingerprint,
            )
            task_evidence = task_scorer.private_audit_evidence
            assert task_score.official is True
            assert task_score.success is False
            assert task_evidence["scoring_status"] == "scored"
            assert task_evidence["verifier_count"] == len(spec.verifiers)
            assert task_evidence["passed_count"] < task_evidence["verifier_count"]
            assert task_evidence["initial_state_fingerprint"] == state_fingerprint
            assert task_evidence["post_state_fingerprint"] == state_fingerprint
            assert task_evidence["forbidden_side_effects"] == "not_measured"
            assert "SELECT" not in repr(task_evidence)
            assert "expected_value" not in repr(task_evidence)
            verifier_run = {
                "reset_index": reset_index + 1,
                "database_fingerprint": state_fingerprint,
                "official_verifier": {
                    "source": "pinned-original-enterpriseops-verifier",
                    "status": task_evidence["scoring_status"],
                    "verifier_count": task_evidence["verifier_count"],
                    "passed_count": task_evidence["passed_count"],
                    "result_digest": task_evidence["verifier_results_digest"],
                    "task_success": task_score.success,
                    "failure_kind": "task_failure",
                    "forbidden_side_effects": "not_measured",
                    "judge_requests": task_score.judge_requests,
                },
            }
            previous_database = trial.database_id
            previous_container = trial._container.container_id
            previous_read = read
            previous_state_fingerprint = state_fingerprint
        assert trial.cleanup_verified is True
        verifier_run["cleanup_verified"] = trial.cleanup_verified
        verifier_runs.append(verifier_run)

    evidence_path = os.getenv("DECAGENT_ENTERPRISEOPS_EVIDENCE_PATH")
    if evidence_path:
        evidence_dir = (ROOT / "evals/benchmarks/evidence").resolve()
        requested = Path(evidence_path)
        target = requested.with_name(
            f"{requested.stem}-{domain}{requested.suffix or '.json'}"
        ).resolve()
        if not target.is_relative_to(evidence_dir) or target.exists():
            raise ValueError("Optional evidence must be a new file under benchmark evidence")
        evidence = {
            "schema_version": "enterpriseops-private-verifier-smoke-v2",
            "observed_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "evidence_type": "local_environment_readiness_and_official_task_verifier",
            "command": (
                "DECAGENT_ENTERPRISEOPS_DOCKER_SMOKE=1 uv run --extra dev pytest "
                "evals/integration/test_business_enterprise_environment.py -q"
            ),
            "domain": domain,
            "task_id": spec.task_id,
            "image_digest": spec.image,
            "result": {"passed": 1, "fresh_resets": len(verifier_runs)},
            "verifier_runs": verifier_runs,
            "model_requests": 0,
            "business_writes": 0,
            "limitations": [
                "The frozen official verifier ran against a fresh unchanged snapshot; "
                "task_failure was expected.",
                "This does not demonstrate successful task completion or any Agent/model quality.",
                "Intermediate forbidden side effects were not measured and remain not_measured.",
            ],
        }
        target.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "domain,read_tool,arguments",
    [
        ("csm", "find_product", {"name": "__decagent_readonly_smoke__"}),
        ("itsm", "list_users", {}),
    ],
)
def test_scripted_first_wave_route_uses_real_tool_official_scorer_and_cleanup(
    tmp_path, domain, read_tool, arguments
):
    from decimal import Decimal

    from decagent.evaluation.business.comparison import Route
    from decagent.evaluation.business.public_session import enterprise_session_factory
    from decagent.evaluation.business.public_trials import load_public_run_plan
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply
    from evals.benchmarks.live_public_runner import (
        _enterprise_boundary,
        _enterprise_task,
        build_public_comparison_trial,
    )

    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    selection = next(
        item
        for item in plan.initial_smoke_wave
        if item["benchmark"] == "enterpriseops-gym" and item["domain"] == domain
    )
    record = next(item for item in plan.trials if item["trial_id"] == selection["trial_id"])
    source = json.loads(
        (ROOT / "evals/benchmarks/manifests/source_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    inventory = json.loads(
        (ROOT / "evals/benchmarks/manifests/task_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    spec, dataset_checksum = _enterprise_task(ROOT, record, source, inventory)
    assert read_tool in spec.selected_tools
    assert read_tool not in spec.restricted_tools
    with EnterpriseEnvironment(spec) as preview:
        _, boundary = _enterprise_boundary(spec, preview, record)
    assert preview.cleanup_verified is True

    trial = build_public_comparison_trial(
        plan,
        record,
        dataset_checksum=dataset_checksum,
        boundary_facts=boundary,
    )
    session_factory = enterprise_session_factory(
        spec,
        record["version"],
        tmp_path,
    )

    from contextlib import contextmanager

    @contextmanager
    def verified_session_factory(current_trial, isolation_id):
        with session_factory(current_trial, isolation_id) as session:
            assert session.spec.commands[read_tool].effect == "read"
            yield session

    scripted_requests = []

    def react(request):
        scripted_requests.append(request)
        if not request["observations"]:
            value = {"tool": read_tool, "arguments": arguments}
        else:
            value = {"finish": True, "message": "I checked the available information."}
        return ModelReply(
            value,
            tokens=8,
            cost_usd=Decimal("0"),
            model="scripted-offline",
            input_tokens=5,
            output_tokens=3,
        )

    result = ControlledRoutes(verified_session_factory).run(
        Route.REACT_SKILL_MCP,
        trial,
        react=react,
    )

    assert result.status == "complete", result.error
    assert result.run is not None and result.run.task_success is False
    assert result.score is not None and result.score.official is True
    assert result.score.success is False
    assert result.usage.primary_requests == 2
    assert len(scripted_requests) == 2
    assert [call.command for call in result.tool_calls] == [read_tool]
    assert result.cleanup_verified is True
    assert result.cleanup_evidence == {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
    }
