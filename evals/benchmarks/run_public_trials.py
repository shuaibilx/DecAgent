"""Preflight the frozen public comparison; this command is offline by default."""

import argparse
import json
import os
import sys
from pathlib import Path

from decagent.evaluation.business.public_trials import (
    build_dry_run_report,
    load_public_run_plan,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    command.add_argument("--mode", choices=("smoke", "full"))
    command.add_argument(
        "--phase",
        choices=("first-wave", "remaining-smoke", "full"),
        help="show the fixed task slice selected for this offline phase",
    )
    command.add_argument("--manifest", type=Path)
    command.add_argument("--source-manifest", type=Path)
    command.add_argument("--model-profiles", type=Path)
    command.add_argument("--readiness-evidence", type=Path)
    command.add_argument(
        "--check-images",
        action="store_true",
        help="also require the digest-pinned EnterpriseOps images to exist locally",
    )
    command.add_argument(
        "--execute",
        action="store_true",
        help="execute only the fixed, supervised first-wave after all live gates pass",
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
        help="write the dry-run report as a new JSON file under evals/benchmarks/evidence",
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


def _execute_live_first_wave(args, command):
    if args.phase != "first-wave":
        command.error("live execution requires --phase first-wave")
    if args.mode not in {None, "smoke"}:
        command.error("live first-wave execution requires --mode smoke")
    if args.state:
        command.error("--state is unsupported; live execution uses the canonical ledger")
    if not args.report:
        command.error("live first-wave execution requires a new --report under benchmark evidence")

    from evals.benchmarks.live_public_runner import (
        _json_ready,
        execute_live_first_wave,
        require_verified_candidate,
        validate_live_report_target,
        write_live_report,
    )

    root = args.root.resolve()
    try:
        validate_live_report_target(root, args.report)
        candidate_sha = require_verified_candidate(root)

        settings = _settings_for_project(root)
        if not settings.main_api_key:
            raise ValueError("DECAGENT_MAIN_API_KEY is required in the server environment")
        if settings.fallback_enabled:
            raise ValueError("Cross-model fallback must be disabled for the frozen comparison")

        tau_python = args.tau_python or Path(
            os.getenv(
                "DECAGENT_TAU_PYTHON",
                root
                / ".decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe",
            )
        )
        tau_python = tau_python.resolve()
        if not tau_python.is_file():
            raise ValueError("The pinned τ-Bench Python executable is unavailable")

        run_plan = load_public_run_plan(
            root,
            mode="smoke",
            manifest_path=args.manifest,
            source_manifest_path=args.source_manifest,
            profiles_path=args.model_profiles,
            readiness_path=args.readiness_evidence,
            check_images=True,
            check_runtime=True,
            check_source_files=True,
        )
        phase, report = execute_live_first_wave(
            run_plan,
            settings=settings,
            tau_python=str(tau_python),
            journal_root=root / ".decagent/evaluation/journals",
            candidate_sha=candidate_sha,
            working_tree_dirty=False,
        )
        report_path = write_live_report(root, args.report, report)
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
    print(f"Live first-wave report: {report_path}", file=sys.stderr)
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
        return _execute_live_first_wave(args, command)
    if args.state:
        command.error("--state is unavailable in dry-run mode; no ledger is created")
    mode = args.mode or ("full" if args.phase == "full" else "smoke")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_plan = load_public_run_plan(
        args.root,
        mode=mode,
        manifest_path=args.manifest,
        source_manifest_path=args.source_manifest,
        profiles_path=args.model_profiles,
        readiness_path=args.readiness_evidence,
        check_images=args.check_images,
        check_runtime=False,
        check_source_files=False,
    )
    try:
        report = build_dry_run_report(run_plan, phase=args.phase)
    except ValueError as exc:
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
