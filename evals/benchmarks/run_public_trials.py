"""Preflight the frozen public comparison; this command is offline by default."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from decagent.evaluation.business.public_trials import (
    build_dry_run_report,
    canonical_public_trial_ledger_path,
    load_public_run_plan,
)
from decagent.evaluation.business.public_weekly_budget import (
    OPENROUTER_CSM_DIAGNOSTIC_AUTHORIZATION_ID,
    complete_openrouter_csm_diagnostic_authorization,
    complete_openrouter_diagnostic_continuation,
    complete_public_evaluation_phase,
)

from evals.benchmarks.pilot_gate import (
    build_bounded_diagnostic_plan,
    build_bounded_openrouter_csm_diagnostic_plan,
    build_bounded_openrouter_diagnostic_plan,
    evaluation_source_digest,
    validate_diagnostic_evidence,
)
from evals.benchmarks.pilot_manifest import load_public_pilot_plan


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    command.add_argument("--mode", choices=("smoke", "full", "pilot"))
    command.add_argument(
        "--phase",
        choices=(
            "diagnostic", "openrouter-diagnostic", "openrouter-csm-diagnostic", "first-wave",
            "remaining-smoke", "full", "pilot",
        ),
        help="show the fixed task slice selected for this offline phase",
    )
    command.add_argument(
        "--diagnostic-round",
        type=int,
        choices=(1, 2),
        default=1,
        help="select the explicitly authorized diagnostic round; round 2 is one-time only",
    )
    command.add_argument("--manifest", type=Path)
    command.add_argument("--pilot-manifest", type=Path)
    command.add_argument("--source-manifest", type=Path)
    command.add_argument("--model-profiles", type=Path)
    command.add_argument("--readiness-evidence", type=Path)
    command.add_argument(
        "--diagnostic-evidence",
        type=Path,
        help="CI-verified bounded diagnostic required before pilot freeze or dispatch",
    )
    command.add_argument(
        "--parent-diagnostic-evidence",
        type=Path,
        help="completed Qwen round-2 report bound to the separate OpenRouter diagnostic",
    )
    command.add_argument(
        "--check-images",
        action="store_true",
        help="also require the digest-pinned EnterpriseOps images to exist locally",
    )
    command.add_argument(
        "--execute",
        action="store_true",
        help="execute only a bounded diagnostic or a gated frozen public pilot",
    )
    command.add_argument(
        "--state",
        type=Path,
        help="unsupported: live execution always uses the canonical ledger",
    )
    command.add_argument(
        "--tau-python",
        type=Path,
        help="dedicated pinned Python 3.12 executable for the τ-Bench worker",
    )
    command.add_argument(
        "--report",
        type=Path,
        help=(
            "write a new JSON report; dry-run uses evals/benchmarks/evidence and live "
            "uses the ignored .decagent/evaluation/evidence directory"
        ),
    )
    return command


def _settings_for_project(root):
    """Load only the selected DecAgent project's dotenv before parsing settings."""
    from decagent.config import Settings
    from dotenv import load_dotenv

    load_dotenv(Path(root).resolve() / ".env", override=False)
    previous = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        return Settings.from_env()
    finally:
        if previous is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = previous


def _load_plan(args, root, *, live=False):
    mode = args.mode or (
        "pilot"
        if args.phase == "pilot"
        else "full"
        if args.phase == "full"
        else "smoke"
    )
    if mode == "pilot":
        return load_public_pilot_plan(
            root,
            pilot_manifest_path=(
                args.pilot_manifest
                or root
                / ".decagent/evaluation/manifests/sop_agent_public_pilot_20261024_v1.json"
            ),
            base_manifest_path=args.manifest,
            source_manifest_path=args.source_manifest,
            profiles_path=args.model_profiles,
            readiness_path=args.readiness_evidence,
            diagnostic_evidence_path=args.diagnostic_evidence,
            check_images=args.check_images or live,
            check_runtime=live,
            check_source_files=live,
        )
    use_fireworks_diagnostic_profile = args.phase in {
        "openrouter-diagnostic",
        "openrouter-csm-diagnostic",
    }
    readiness_path = args.readiness_evidence
    if use_fireworks_diagnostic_profile and readiness_path is None:
        readiness_path = (
            root
            / "evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.json"
        )
    return load_public_run_plan(
        root,
        mode=mode,
        manifest_path=(
            args.manifest
            or (
                root / "evals/benchmarks/manifests/comparison_trial_manifest_v12.json"
                if use_fireworks_diagnostic_profile
                else None
            )
        ),
        source_manifest_path=args.source_manifest,
        profiles_path=(
            args.model_profiles
            or (
                root / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json"
                if use_fireworks_diagnostic_profile
                else None
            )
        ),
        readiness_path=readiness_path,
        # The diagnostic plan may be inspected offline before a matched profile
        # exists. Live dispatch always requires provider-specific readiness.
        allow_unready_diagnostic_model=(
            use_fireworks_diagnostic_profile and not live
        ),
        check_images=args.check_images or live,
        check_runtime=live,
        check_source_files=live,
    )


def _execute_live_phase(args, command):
    if args.phase not in {
        "diagnostic", "openrouter-diagnostic", "openrouter-csm-diagnostic", "pilot"
    }:
        command.error(
            "unbounded historical live phases are closed; use --phase diagnostic, "
            "--phase openrouter-diagnostic, --phase openrouter-csm-diagnostic, or "
            "--phase pilot"
        )
    required_mode = "pilot" if args.phase == "pilot" else "smoke"
    if args.mode not in {None, required_mode}:
        command.error(f"live {args.phase} execution requires --mode {required_mode}")
    if args.phase == "pilot" and not args.diagnostic_evidence:
        command.error("live pilot execution requires --diagnostic-evidence")
    if args.phase == "openrouter-diagnostic" and not args.parent_diagnostic_evidence:
        command.error(
            "OpenRouter diagnostic execution requires --parent-diagnostic-evidence"
        )
    if args.phase != "diagnostic" and args.diagnostic_round != 1:
        command.error("--diagnostic-round applies only to the diagnostic phase")
    if args.state:
        command.error("--state is unsupported; live execution uses the canonical ledger")
    if not args.report:
        command.error("live evaluation requires a new --report under benchmark evidence")

    from evals.benchmarks.live_public_runner import (
        _json_ready,
        execute_live_public_phase,
        require_verified_candidate,
        validate_live_report_target,
        write_live_report,
    )

    root = args.root.resolve()
    try:
        validate_live_report_target(root, args.report)
        candidate_sha = require_verified_candidate(root)

        settings = _settings_for_project(root)
        if args.phase in {"openrouter-diagnostic", "openrouter-csm-diagnostic"}:
            if not settings.resolved_fallback_api_key:
                raise ValueError(
                    "The OpenRouter API key is required in the server environment"
                )
        elif not settings.main_api_key:
            raise ValueError("DECAGENT_MAIN_API_KEY is required in the server environment")
        if settings.fallback_enabled:
            raise ValueError("Cross-model fallback must be disabled for the frozen comparison")

        run_plan = _load_plan(args, root, live=True)
        if args.phase == "diagnostic":
            run_plan = build_bounded_diagnostic_plan(run_plan)
        elif args.phase == "openrouter-diagnostic":
            from evals.benchmarks.live_public_runner import (
                validate_openrouter_parent_evidence,
            )

            parent_evidence = validate_openrouter_parent_evidence(
                root, args.parent_diagnostic_evidence
            )
            run_plan = build_bounded_openrouter_diagnostic_plan(run_plan)
        elif args.phase == "openrouter-csm-diagnostic":
            run_plan = build_bounded_openrouter_csm_diagnostic_plan(run_plan)
        else:
            validate_diagnostic_evidence(
                root,
                args.diagnostic_evidence,
                model_profiles_digest=run_plan.profiles.fingerprint,
                expected_source_digest=evaluation_source_digest(root),
                expected_source_manifest_digest=run_plan.source_manifest_digest,
                expected_inventory_digest=run_plan.inventory_digest,
            )
        tau_python = args.tau_python or Path(
            os.getenv(
                "DECAGENT_TAU_PYTHON",
                root
                / ".decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe",
            )
        )
        tau_python = tau_python.resolve()
        if any(record["benchmark"] == "tau-bench" for record in run_plan.trials):
            if not tau_python.is_file():
                raise ValueError("The pinned τ-Bench Python executable is unavailable")
        else:
            tau_python = None
        call = {
            "settings": settings,
            "tau_python": str(tau_python) if tau_python is not None else None,
            "journal_root": root / ".decagent/evaluation/journals",
            "candidate_sha": candidate_sha,
            "working_tree_dirty": False,
            "candidate_ci_verified": True,
            "phase": args.phase,
            "diagnostic_round": args.diagnostic_round,
        }
        if args.phase == "openrouter-diagnostic":
            call["parent_report_sha256"] = parent_evidence["report_sha256"]
        if args.phase == "pilot":
            call["diagnostic_evidence"] = args.diagnostic_evidence
        phase, report = execute_live_public_phase(run_plan, **call)
        report_path = write_live_report(root, args.report, report)
        if (
            report.get("phase") != args.phase
            or report.get("evaluation_budget_id")
            != (
                OPENROUTER_CSM_DIAGNOSTIC_AUTHORIZATION_ID
                if args.phase == "openrouter-csm-diagnostic"
                else "sop-pilot-20261024-v1"
            )
            or type(report.get("cohort_digest")) is not str
            or type(report.get("candidate_sha")) is not str
        ):
            raise ValueError("Live report is missing its weekly budget identity")
        diagnostic_report_sha256 = None
        if args.phase == "pilot":
            gate = report.get("diagnostic_gate")
            diagnostic_report_sha256 = (
                gate.get("report_sha256") if type(gate) is dict else None
            )
        ledger_path = canonical_public_trial_ledger_path(
            root,
            cohort_digest=report["cohort_digest"],
            candidate_sha=report["candidate_sha"],
            phase=(
                "openrouter-csm-diagnostic"
                if args.phase == "openrouter-csm-diagnostic"
                else None
            ),
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if args.phase == "openrouter-diagnostic":
            complete_openrouter_diagnostic_continuation(
                root,
                parent_report_sha256=parent_evidence["report_sha256"],
                candidate_sha=report["candidate_sha"],
                cohort_digest=report["cohort_digest"],
                ledger_path=ledger_path,
                report_sha256=report_sha256,
            )
        elif args.phase == "openrouter-csm-diagnostic":
            complete_openrouter_csm_diagnostic_authorization(
                root,
                report["authorization_record"],
                report_sha256=report_sha256,
            )
        else:
            complete_public_evaluation_phase(
                root,
                phase=args.phase,
                phase_round=args.diagnostic_round,
                candidate_sha=report["candidate_sha"],
                cohort_digest=report["cohort_digest"],
                ledger_path=ledger_path,
                report_sha256=report_sha256,
                diagnostic_report_sha256=diagnostic_report_sha256,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        command.error(str(exc))

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(
        json.dumps(
            _json_ready(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    print(f"Live {args.phase} report: {report_path}", file=sys.stderr)
    return 0


def _write_dry_run_report(root, target, report):
    root = Path(root).resolve()
    evidence = (root / "evals/benchmarks/evidence").resolve()
    target = Path(target)
    requested = (root / target) if not target.is_absolute() else target
    if requested.is_symlink():
        raise ValueError("Dry-run report target cannot be a symlink")
    target = requested.resolve()
    if (
        not target.is_relative_to(evidence)
        or target.suffix.lower() != ".json"
    ):
        raise ValueError("Dry-run reports must be new JSON files under benchmark evidence")
    if not target.parent.is_dir():
        raise ValueError("Dry-run evidence directory must already exist")
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return target


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    if args.execute:
        return _execute_live_phase(args, command)
    if args.state:
        command.error("--state is unavailable in dry-run mode; no ledger is created")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        run_plan = _load_plan(args, args.root.resolve(), live=False)
        if args.phase == "diagnostic":
            run_plan = build_bounded_diagnostic_plan(run_plan)
        elif args.phase == "openrouter-diagnostic":
            run_plan = build_bounded_openrouter_diagnostic_plan(run_plan)
        elif args.phase == "openrouter-csm-diagnostic":
            run_plan = build_bounded_openrouter_csm_diagnostic_plan(run_plan)
        report = build_dry_run_report(run_plan, phase=args.phase)
    except (OSError, ValueError) as exc:
        command.error(str(exc))
    if args.report:
        try:
            _write_dry_run_report(args.root, args.report, report)
        except (OSError, ValueError) as exc:
            command.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
