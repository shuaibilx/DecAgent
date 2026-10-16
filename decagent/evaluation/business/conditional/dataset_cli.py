"""Standalone dataset CLI; never imports a model transport or database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .datasets import (
    SOURCES,
    DatasetUnavailable,
    acquire_dataset,
    freeze_prepared,
    import_dataset,
    prepare_dataset,
    source_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "fetch", "prepare"))
    parser.add_argument("--source", choices=sorted(SOURCES), required=True)
    parser.add_argument("--root", type=Path, default=Path(".decagent/conditional-evaluation/data"))
    parser.add_argument("--data", type=Path, help="Explicit local pinned data file")
    parser.add_argument("--license", type=Path, help="Explicit local pinned license file")
    parser.add_argument("--output", type=Path, help="New frozen dataset file; never overwritten")
    args = parser.parse_args(argv)
    try:
        config = source_config(args.source)
        if args.command == "config":
            result = config
        elif args.command == "fetch":
            result = acquire_dataset(args.source, args.root)
        else:
            folder = args.root / args.source
            imported = import_dataset(
                args.source,
                args.data or folder / config["filename"],
                args.license or folder / config["license"]["filename"],
            )
            prepared = prepare_dataset(imported)
            acquisition = folder / "acquisition.json"
            if acquisition.is_file():
                prepared["manifest"]["acquisition"] = json.loads(
                    acquisition.read_text(encoding="utf-8")
                )
            output = args.output or folder / "frozen.json"
            freeze_prepared(prepared, output)
            result = {
                "output": str(output.resolve()),
                "split_counts": prepared["manifest"]["split_counts"],
                "subset_counts": prepared["manifest"]["subset_counts"],
                "shortfalls": prepared["shortfalls"],
                "stratification_shortfalls": prepared["manifest"]["stratification_shortfalls"],
                "evidence_mode": "offline-data-only",
            }
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (DatasetUnavailable, ValueError, OSError) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
