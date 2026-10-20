"""Offline by default; external evaluation requires --live and a durable ledger."""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from decagent.business.codec import strict_json
from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest

from .calibration import fit_export
from .dataset import DEFAULT_DATASET, freeze_dataset, load_dataset, select_cases
from .ledger import GlobalJevLedger, validate_global_jev_ledger_path
from .metrics import build_report
from .schedule import make_schedule
from .thresholds import (
    cache_from_calibration_export,
    read_prediction_cache,
    registered_business_families,
    scan_threshold_grid,
    write_prediction_cache,
)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    commands = result.add_subparsers(dest="command")
    commands.add_parser("inspect", help="Verify the frozen dataset; no database or model calls")
    generate = commands.add_parser("generate", help="Freeze a new copy; never overwrite")
    generate.add_argument("--output", type=Path, required=True)
    initialize = commands.add_parser("ledger-init", help="Persist historical $0.000253176 spend")
    initialize.add_argument("--ledger", type=Path, required=True)
    report = commands.add_parser("report", help="Recompute a saved report offline")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path)
    eligibility = commands.add_parser(
        "eligibility-diagnostic", help="Check frozen auxiliary sample gates; no model calls"
    )
    eligibility.add_argument("--output", type=Path, required=True)
    fit = commands.add_parser(
        "calibrate-fit", help="Fit only a complete development export offline"
    )
    fit.add_argument("--input", type=Path, required=True)
    fit.add_argument("--output", type=Path, required=True)
    cache = commands.add_parser(
        "calibrate-cache", help="Freeze development predictions for offline threshold scans"
    )
    cache.add_argument("--input", type=Path, required=True)
    cache.add_argument("--output", type=Path, required=True)
    scan = commands.add_parser(
        "threshold-scan", help="Scan saved Jev predictions without additional model calls"
    )
    scan.add_argument("--input", type=Path, required=True)
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--target-accuracy", type=float, default=0.95)
    scan.add_argument("--minimum-auto", type=int, default=100)
    scan.add_argument("--threshold", type=float, action="append")
    for name in ("run", "calibrate-collect"):
        live = commands.add_parser(name, help="Explicit external calls on a NEW benchmark database")
        live.add_argument("--live", action="store_true", required=True)
        live.add_argument("--ledger", type=Path, required=True)
        live.add_argument(
            "--source-database",
            required=True,
            help="Disposable test/admin DSN; only creates a NEW businessbench database",
        )
        live.add_argument("--output", type=Path, required=True)
        live.add_argument(
            "--max-trials" if name == "run" else "--max-cases",
            type=int,
            choices=range(1, 41) if name == "calibrate-collect" else None,
        )
        live.add_argument("--jev-price-version")
        live.add_argument("--jev-input-per-million", type=Decimal)
        live.add_argument("--jev-output-per-million", type=Decimal)
        if name == "run":
            live.add_argument("--profile", type=Path, required=True)
            live.add_argument(
                "--main-free-quota",
                action="store_true",
                help="Owner authorizes bounded unknown-price main calls using free quota",
            )
    return result


def _new_output(path):
    if path.exists():
        raise FileExistsError("Output already exists; choose a new evidence path")


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    command = args.command or "inspect"
    if hasattr(args, "ledger"):
        project_root = Path(__file__).resolve().parents[3]
        args.ledger = validate_global_jev_ledger_path(project_root, args.ledger)
    if command == "generate":
        _print({"path": str(freeze_dataset(args.output))})
        return 0
    if command == "ledger-init":
        ledger = GlobalJevLedger(args.ledger)
        ledger.initialize()
        _print(ledger.snapshot())
        return 0
    if command == "eligibility-diagnostic":
        from .eligibility import development_eligibility_report

        _new_output(args.output)
        report = development_eligibility_report(workflow_path=args.dataset)
        _write(args.output, report)
        _print({"report": str(args.output), "status": report["status"], "live_calls": 0})
        return 0
    dataset = load_dataset(args.dataset)
    if command == "inspect":
        _print(
            {k: dataset[k] for k in ("schema_version", "version", "checksum", "label_source")}
            | {
                "development": 40,
                "holdout": 80,
                "holdout_kinds": {
                    k: sum(c["kind"] == k for c in select_cases(dataset, "holdout"))
                    for k in ("common", "complex", "edge")
                },
                "live_calls": 0,
            }
        )
        return 0
    if command == "calibrate-fit":
        _new_output(args.output)
        export = strict_json(args.input.read_text(encoding="utf-8"))
        profile = fit_export(dataset, export, PluginSpec.from_mapping(builtin_manifest()))
        _write(args.output, profile.to_dict())
        _print(
            {
                "profile": str(args.output),
                "model": profile.model,
                "label_source": profile.label_source,
                "holdout_used": False,
            }
        )
        return 0
    if command == "calibrate-cache":
        _new_output(args.output)
        export = strict_json(args.input.read_text(encoding="utf-8"))
        spec = PluginSpec.from_mapping(builtin_manifest())
        cache = cache_from_calibration_export(dataset, export, spec)
        write_prediction_cache(args.output, cache)
        _print(
            {
                "prediction_cache": str(args.output),
                "checksum": cache["checksum"],
                "records": len(cache["records"]),
                "model_calls": 0,
                "label_source": cache["metadata"]["label_source"],
            }
        )
        return 0
    if command == "threshold-scan":
        _new_output(args.output)
        cache = read_prediction_cache(args.input)
        if (
            cache["metadata"]["dataset_version"] != dataset["version"]
            or cache["metadata"]["dataset_checksum"] != dataset["checksum"]
        ):
            raise ValueError("Prediction cache belongs to another frozen dataset")
        spec = PluginSpec.from_mapping(builtin_manifest())
        report = scan_threshold_grid(
            cache,
            registered_families=registered_business_families(spec),
            target_accuracy=args.target_accuracy,
            minimum_auto=args.minimum_auto,
            thresholds=args.threshold or (0.0, 0.50, 0.70, 0.80, 0.90, 0.95, 0.99),
        )
        _write(args.output, report)
        _print(
            {
                "report": str(args.output),
                "split": report["split"],
                "families": len(report["families"]),
                "live_calls": 0,
            }
        )
        return 0
    if command == "report":
        saved = strict_json(args.input.read_text(encoding="utf-8"))
        metadata = saved["metadata"]
        if metadata["dataset_checksum"] != dataset["checksum"]:
            raise ValueError("Saved report belongs to another frozen dataset")
        cases = select_cases(dataset, metadata["split"])
        rounds = 3 if saved["planned_trials"] == len(cases) * 9 else 1
        report = build_report(
            make_schedule(cases, rounds=rounds),
            saved["records"],
            evidence_mode=saved["evidence_mode"],
            metadata=metadata,
        )
        if metadata["split"] != "holdout" or report["planned_trials"] != 720:
            report["acceptance"]["checks"]["full_frozen_holdout_three_rounds"] = False
            report["acceptance"]["status"] = "not_passed"
        if args.output:
            _new_output(args.output)
            _write(args.output, report)
        _print(report)
        return 0
    raise ValueError(
        "Live v1 CLI evaluation is retired; use the fixed authenticated MCP acceptance suite. "
        "Saved v1 reports and offline calibration analysis retain their original scope."
    )


if __name__ == "__main__":
    raise SystemExit(main())
