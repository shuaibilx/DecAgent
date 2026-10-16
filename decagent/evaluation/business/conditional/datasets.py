"""Pinned public semantic data, lexical group isolation, and budget subsets.

This module uses only the standard library. Raw data stays outside the package.
The frozen output includes hidden scoring labels; always call ``model_input``
when constructing a model request. Grouping is a documented lexical heuristic,
not a claim to detect all semantic paraphrases or pretraining contamination.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import random
import re
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SEED = 20261015
SPLITS = ("development", "validation", "test")
GROUPING_VERSION = "lexical-components-1"
CLINC_COMMIT = "828f8093932c8fe6ca7936c3d2e52903b1c523de"
KAGGLE_REF = "bitext/training-dataset-for-chatbotsvirtual-assistants"
BITEXT_DIRECTORY = (
    "20000-Utterances-Training-dataset-for-chatbots-virtual-assistant-Bitext-sample/"
    "20000-Utterances-Training-dataset-for-chatbots-virtual-assistant-Bitext-sample/"
)
BITEXT_MEMBER = BITEXT_DIRECTORY + (
    "20000-Utterances-Training-dataset-for-chatbots-virtual-assistant-Bitext-sample.csv"
)
CLINC_BASE = f"https://raw.githubusercontent.com/clinc/oos-eval/{CLINC_COMMIT}/"
SOURCES = {
    "kaggle-bitext": {
        "id": "kaggle-bitext",
        "kind": "bitext",
        "publisher": "Bitext Innovations",
        "source_url": f"https://www.kaggle.com/datasets/{KAGGLE_REF}/metadata",
        "metadata_url": f"https://www.kaggle.com/api/v1/datasets/view/{KAGGLE_REF}",
        "version": "2",
        "version_published_at": "2022-03-17T16:34:57.633Z",
        "download_url": (
            f"https://www.kaggle.com/api/v1/datasets/download/{KAGGLE_REF}?datasetVersionNumber=2"
        ),
        "archive_sha256": "f9bc8f3991e769515680ced429fa80bc60a98a7cdb9c1c82bcf33abd53413a1b",
        "archive_member": BITEXT_MEMBER,
        "filename": "bitext-v2-20000.csv",
        "sha256": "19a3031d649ce432202acd807630b1b52884507185f32832d58714f3fac7d82b",
        "official_splits": False,
        "language": "en",
        "redistribute_raw": False,
        "field_mapping": {
            "input": "utterance",
            "hidden_label": "intent",
            "discard": ["flags", "category", "response"],
        },
        "license": {
            "id": "CDLA-Sharing-1.0",
            "verified": True,
            "filename": "LICENSE.txt",
            "url": "https://cdla.dev/sharing-1-0/",
            "evidence_url": (
                f"https://www.kaggle.com/api/v1/datasets/download/{KAGGLE_REF}"
                "?datasetVersionNumber=2"
            ),
            "archive_member": BITEXT_DIRECTORY + "LICENSE.txt",
            "sha256": "f6054388eae4947a769ca2020c54e6d394eb01c41d58db55bb6cfd24ce71cdd8",
            "note": "Archive sibling LICENSE is explicit; Kaggle metadata only says "
            "Data files copyright Original Authors. No HF license substitution.",
        },
        "readme": {
            "filename": "README.txt",
            "archive_member": BITEXT_DIRECTORY + "README.txt",
            "sha256": "e49c3ac7d24b3c982233ec9a1c68d8f762ed5ab958a4e63966ab2fa9628dad84",
        },
    },
    "clinc150": {
        "id": "clinc150",
        "kind": "clinc",
        "publisher": "Larson et al. / clinc",
        "source_url": "https://github.com/clinc/oos-eval",
        "version": CLINC_COMMIT,
        "download_url": CLINC_BASE + "data/data_full.json",
        "filename": "data_full.json",
        "sha256": "36923c3705a59e08fe9c3883d8bc2dd966ef93e22cb78ac41171782a698d56e0",
        "official_splits": True,
        "language": "en",
        "redistribute_raw": False,
        "field_mapping": {
            "input": "[0]",
            "hidden_label": "[1]",
            "split_keys": {
                "train": "development",
                "val": "validation",
                "test": "test",
                "oos_train": "development",
                "oos_val": "validation",
                "oos_test": "test",
            },
        },
        "license": {
            "id": "CC-BY-3.0",
            "verified": True,
            "filename": "LICENSE",
            "url": "https://creativecommons.org/licenses/by/3.0/",
            "evidence_url": CLINC_BASE + "LICENSE",
            "sha256": "e6bc9e9c474700b708f568bac9e5a8a9bcb2b1dad53442f5ba449fcb848b8e76",
        },
        "readme": {
            "filename": "README.md",
            "url": CLINC_BASE + "README.md",
            "sha256": "8a8df26c4de3d25b6c4cff385ca23602c30e4e56828b015206cbc202f6db363a",
        },
    },
}


class DatasetUnavailable(ValueError):
    """Acquisition/import is pending; it must not be reported as a benchmark."""


def source_config(source_id: str) -> dict:
    try:
        return copy.deepcopy(SOURCES[source_id])
    except KeyError as exc:
        raise ValueError(f"unknown data source: {source_id}") from exc


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _checked_file(path: Path, expected: str, role: str) -> bytes:
    if not path.is_file():
        raise DatasetUnavailable(f"{role} file missing: {path}; acquisition pending")
    raw = path.read_bytes()
    if sha256(raw) != expected:
        raise DatasetUnavailable(f"{role} SHA256 mismatch: {path}; source/version not verified")
    return raw


def verify_license(config: dict, path: Path) -> bytes:
    license_info = config.get("license", {})
    if not license_info.get("verified") or not license_info.get("id"):
        raise DatasetUnavailable("license is not verified; data acquisition pending")
    return _checked_file(Path(path), license_info["sha256"], "license")


def _record(identifier: str, utterance: str, label: str, split: str | None) -> dict:
    if not isinstance(utterance, str) or not utterance.strip():
        raise ValueError(f"invalid utterance at {identifier}")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"invalid hidden label at {identifier}")
    return {"id": identifier, "utterance": utterance, "label": label, "official_split": split}


def parse_bitext(raw: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not {"utterance", "intent"} <= set(reader.fieldnames or ()):
        raise ValueError("Bitext requires utterance and intent fields")
    return [
        _record(f"bitext:{index:06d}", row["utterance"], row["intent"], None)
        for index, row in enumerate(reader)
    ]


def parse_clinc(raw: bytes) -> list[dict]:
    payload = json.loads(raw)
    mapping = SOURCES["clinc150"]["field_mapping"]["split_keys"]
    if not isinstance(payload, dict) or set(payload) != set(mapping):
        raise ValueError("CLINC official split keys are missing or unknown")
    records = []
    for key, split in mapping.items():
        for index, pair in enumerate(payload[key]):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"invalid CLINC pair in {key}:{index}")
            if key.startswith("oos_") != (pair[1] == "oos"):
                raise ValueError(f"CLINC in-scope/oos label mismatch in {key}:{index}")
            records.append(_record(f"clinc:{key}:{index:06d}", pair[0], pair[1], split))
    return records


def import_dataset(source_id: str, raw_path: Path, license_path: Path) -> dict:
    """Import only exact pinned source bytes with their local license evidence."""
    config = source_config(source_id)
    raw_path = Path(raw_path)
    if not raw_path.is_file():
        raise DatasetUnavailable(f"data file missing: {raw_path}; acquisition pending")
    verify_license(config, Path(license_path))
    raw = _checked_file(raw_path, config["sha256"], "data")
    records = parse_bitext(raw) if config["kind"] == "bitext" else parse_clinc(raw)
    labels = sorted({row["label"] for row in records})
    manifest = {
        **config,
        "status": "verified-local-data",
        "raw_bytes": len(raw),
        "row_count": len(records),
        "label_version": sha256(_canonical(labels)),
        "labels": labels,
        "imported_at": datetime.now(UTC).isoformat(),
    }
    return {"manifest": manifest, "records": records}


def model_input(record: dict, *, options: list[str] | None = None) -> dict:
    """The sole model-input adapter: never copy labels or source metadata."""
    result = {"utterance": record["utterance"]}
    if options is not None:
        if any(not isinstance(option, str) for option in options):
            raise ValueError("legal options must be strings")
        result["options"] = list(options)
    return result


def normalize_text(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def _template_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    # Recognizable variable slots only; no intent/category/response/flags used.
    text = re.sub(r"\{\{.*?\}\}|\{[^{}]+\}|<[^<>]+>|\[[^\[\]]+\]", " SLOT ", text)
    text = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+\.\w+|\b\d+(?:[.,:/-]\d+)*\b", " SLOT ", text)
    return normalize_text(text)


def group_records(records: list[dict]) -> list[dict]:
    """Connected components of normalized/template equals and one-token edits.

    Near duplicate rule: equal token sequences after deleting at most one token
    on either side, each original has >=10 tokens. This conservative lexical
    rule catches insertions, deletions and substitutions without quadratic
    fuzzy comparisons. It does not certify all semantic paraphrases absent.
    """
    rows = sorted((copy.deepcopy(row) for row in records), key=lambda row: row["id"])
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate sample IDs")
    parent = list(range(len(rows)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        parent[find(left)] = find(right)

    signatures = {}
    templates = []
    for index, row in enumerate(rows):
        template = _template_text(row["utterance"])
        if not template:
            raise ValueError(f"empty normalized utterance at {row['id']}")
        templates.append(template)
        tokens = template.split()
        keys = [("template", template)]
        if len(tokens) >= 10:
            keys += [
                ("edit", " ".join(tokens[:position] + tokens[position + 1 :]))
                for position in range(len(tokens))
            ]
            keys.append(("edit", template))
        for signature in keys:
            if signature in signatures:
                union(index, signatures[signature])
            else:
                signatures[signature] = index
    members = defaultdict(list)
    for index, template in enumerate(templates):
        members[find(index)].append(template)
    group_ids = {
        root: "g:" + sha256(_canonical(sorted(set(texts))))[:24] for root, texts in members.items()
    }
    for index, row in enumerate(rows):
        row["group_id"] = group_ids[find(index)]
    return rows


def _rank(seed: int, value: str) -> str:
    return sha256(f"{seed}:{value}".encode())


def _assign_bitext(groups: dict, seed: int) -> dict:
    counts = Counter(row["label"] for rows in groups.values() for row in rows)
    assigned = {split: Counter() for split in SPLITS}
    ratios = dict(zip(SPLITS, (0.6, 0.2, 0.2), strict=True))
    result = {split: [] for split in SPLITS}
    ordered = sorted(groups, key=lambda key: (-len(groups[key]), _rank(seed, key)))
    for key in ordered:
        additions = Counter(row["label"] for row in groups[key])

        def change(split, additions=additions):
            # Greedily minimize per-label squared error; groups remain atomic.
            return sum(
                (assigned[split][label] + amount - counts[label] * ratios[split]) ** 2
                - (assigned[split][label] - counts[label] * ratios[split]) ** 2
                for label, amount in additions.items()
            )

        selected = min(SPLITS, key=lambda split: (change(split), SPLITS.index(split)))
        result[selected].extend(groups[key])
        assigned[selected].update(additions)
    return result


def _stratified(rows: list[dict], target: int, seed: int) -> list[dict]:
    by_label = defaultdict(list)
    for row in rows:
        by_label[row["label"]].append(row)
    rng = random.Random(seed)
    for label in sorted(by_label):
        by_label[label].sort(key=lambda row: row["id"])
        rng.shuffle(by_label[label])
    selected = []
    # Balanced round robin: all available intent classes before repeating one.
    while len(selected) < target and by_label:
        for label in sorted(list(by_label)):
            if len(selected) == target:
                break
            selected.append(by_label[label].pop())
            if not by_label[label]:
                del by_label[label]
    return sorted(selected, key=lambda row: row["id"])


def prepare_records(
    records: list[dict],
    *,
    kind: str,
    seed: int = SEED,
    validation_size: int | None = None,
    test_size: int | None = None,
    validation_oos: int = 100,
    test_oos: int = 200,
) -> dict:
    """Split data and choose budget subsets. Inputs may be offline fixtures.

    This pure function does not claim source verification. Formal callers must
    attach the verified ``import_dataset`` manifest before freezing.
    """
    if kind not in {"bitext", "clinc"}:
        raise ValueError("unknown dataset kind")
    sizes = {
        "validation": validation_size
        if validation_size is not None
        else (270 if kind == "bitext" else 400),
        "test": test_size if test_size is not None else (540 if kind == "bitext" else 800),
    }
    if any(size < 0 for size in (*sizes.values(), validation_oos, test_oos)):
        raise ValueError("subset sizes must be nonnegative")
    if kind == "clinc" and any(row.get("official_split") not in SPLITS for row in records):
        raise ValueError("CLINC requires official split on every sample")
    grouped = group_records(records)
    all_groups = defaultdict(list)
    for row in grouped:
        all_groups[row["group_id"]].append(row)
    excluded = []
    duplicate_count = 0
    usable = {}
    for key, members in sorted(all_groups.items()):
        if kind == "clinc" and len({row["official_split"] for row in members}) > 1:
            excluded.extend({**row, "reason": "cross_official_split_group"} for row in members)
            continue
        labels_by_text = defaultdict(set)
        for row in members:
            labels_by_text[normalize_text(row["utterance"])].add(row["label"])
        if any(len(labels) > 1 for labels in labels_by_text.values()):
            excluded.extend({**row, "reason": "conflicting_normalized_labels"} for row in members)
            continue
        unique = {}
        for row in members:
            normalized = normalize_text(row["utterance"])
            if normalized in unique:
                duplicate_count += 1
                excluded.append(
                    {
                        **row,
                        "reason": "normalized_duplicate",
                        "retained_id": unique[normalized]["id"],
                    }
                )
            else:
                unique[normalized] = row
        usable[key] = list(unique.values())
    if kind == "bitext":
        splits = _assign_bitext(usable, seed)
    else:
        splits = {split: [] for split in SPLITS}
        for members in usable.values():
            splits[members[0]["official_split"]].extend(members)
    splits = {split: sorted(rows, key=lambda row: row["id"]) for split, rows in splits.items()}
    subsets = {}
    shortfalls = []
    oos_targets = {"validation": validation_oos, "test": test_oos}
    for split, target in sizes.items():
        if kind == "clinc":
            oos_target = min(target, oos_targets[split])
            in_target = target - oos_target
            oos = _stratified(
                [row for row in splits[split] if row["label"] == "oos"], oos_target, seed
            )
            ins = _stratified(
                [row for row in splits[split] if row["label"] != "oos"], in_target, seed
            )
            subset = sorted(oos + ins, key=lambda row: row["id"])
            if len(oos) < oos_target or len(ins) < in_target:
                shortfalls.append(
                    {
                        "split": split,
                        "requested": target,
                        "actual": len(subset),
                        "requested_oos": oos_target,
                        "actual_oos": len(oos),
                        "requested_in_scope": in_target,
                        "actual_in_scope": len(ins),
                    }
                )
        else:
            subset = _stratified(splits[split], target, seed)
            if len(subset) < target:
                shortfalls.append({"split": split, "requested": target, "actual": len(subset)})
        subsets[split] = subset
    all_labels = sorted({row["label"] for row in records})
    in_scope_labels = [label for label in all_labels if label != "oos"]
    stratification_shortfalls = []
    for split, target in sizes.items():
        labels = in_scope_labels if kind == "clinc" else all_labels
        semantic_target = target - min(target, oos_targets[split]) if kind == "clinc" else target
        nominal_minimum = semantic_target // len(labels) if labels else 0
        actual_counts = Counter(row["label"] for row in subsets[split])
        for label in labels:
            if actual_counts[label] < nominal_minimum:
                stratification_shortfalls.append(
                    {
                        "split": split,
                        "label": label,
                        "nominal_minimum": nominal_minimum,
                        "actual": actual_counts[label],
                        "available_in_split": sum(row["label"] == label for row in splits[split]),
                        "reason": "insufficient same-split class samples; other classes fill total",
                    }
                )
    manifest = {
        "schema_version": "conditional-public-data-1",
        "evidence_mode": "offline-data-only",
        "source_verified": False,
        "kind": kind,
        "seed": seed,
        "grouping_version": GROUPING_VERSION,
        "grouping_rules": [
            "NFKC/casefold/punctuation-whitespace normalization",
            "recognizable placeholder/email/URL/number template slots",
            "one-token edit signatures on sequences of at least 10 tokens",
            "transitive text-only connected components",
        ],
        "split_rule": "official splits; conflict groups excluded"
        if kind == "clinc"
        else "group-isolated 60/20/20 greedy label stratification",
        "sampling_rule": "seeded balanced label round-robin within split; no cross-split fill",
        "raw_rows": len(records),
        "usable_rows": sum(map(len, splits.values())),
        "group_count": len(usable),
        "deduplicated_rows": duplicate_count,
        "excluded_count": len(excluded),
        "split_counts": {split: len(rows) for split, rows in splits.items()},
        "split_label_counts": {
            split: dict(sorted(Counter(row["label"] for row in rows).items()))
            for split, rows in splits.items()
        },
        "split_ids": {split: [row["id"] for row in rows] for split, rows in splits.items()},
        "subset_ids": {split: [row["id"] for row in rows] for split, rows in subsets.items()},
        "subset_counts": {split: len(rows) for split, rows in subsets.items()},
        "subset_label_counts": {
            split: dict(sorted(Counter(row["label"] for row in rows).items()))
            for split, rows in subsets.items()
        },
        "subset_requested": sizes,
        "stratification_shortfalls": stratification_shortfalls,
        "model_input_fields": ["utterance", "legal options"],
        "limitations": [
            "English public intent data; no workflow or Chinese success claim",
            "Lexical grouping cannot prove absence of unrecognized paraphrases",
            "Public test content may occur in model pretraining",
            "Budget subsets are not official full-dataset benchmark results",
        ],
    }
    manifest["sample_list_sha256"] = sha256(
        _canonical({"splits": manifest["split_ids"], "subsets": manifest["subset_ids"]})
    )
    return {
        "manifest": manifest,
        "splits": splits,
        "subsets": subsets,
        "excluded": sorted(excluded, key=lambda row: row["id"]),
        "shortfalls": shortfalls,
    }


def prepare_dataset(imported: dict) -> dict:
    config = imported["manifest"]
    prepared = prepare_records(imported["records"], kind=config["kind"])
    prepared["manifest"]["source_verified"] = True
    prepared["manifest"]["source"] = copy.deepcopy(config)
    return prepared


def freeze_prepared(prepared: dict, path: Path) -> None:
    document = copy.deepcopy(prepared)
    document["checksum"] = sha256(_canonical(prepared))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def load_prepared(path: Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    checksum = document.pop("checksum", None)
    if checksum != sha256(_canonical(document)):
        raise ValueError("frozen dataset checksum mismatch")
    return document


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "DecAgent-public-datasets/1"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise DatasetUnavailable(f"public data acquisition pending: {url}: {exc}") from exc


def acquire_dataset(source_id: str, root: Path) -> dict:
    """Anonymous downloads only. Exact hashes and license block changed releases."""
    config = source_config(source_id)
    folder = Path(root) / source_id
    folder.mkdir(parents=True, exist_ok=True)
    acquisition_path = folder / "acquisition.json"
    if acquisition_path.exists():
        raise FileExistsError(f"acquisition already exists: {acquisition_path}")
    events = []

    def save(name, raw, url, expected=None):
        if expected is not None and sha256(raw) != expected:
            raise DatasetUnavailable(f"download SHA256 mismatch: {name}; acquisition pending")
        path = folder / name
        if path.exists() and path.read_bytes() != raw:
            raise FileExistsError(f"refusing to overwrite different data: {path}")
        path.write_bytes(raw)
        events.append(
            {
                "filename": name,
                "url": url,
                "sha256": sha256(raw),
                "bytes": len(raw),
                "downloaded_at": datetime.now(UTC).isoformat(),
            }
        )

    acquisition = {
        "source_id": source_id,
        "version": config["version"],
        "files": events,
        "status": "pending",
        "source_url": config["source_url"],
    }
    try:
        if config["kind"] == "bitext":
            metadata = _fetch(config["metadata_url"])
            save("source-metadata.json", metadata, config["metadata_url"])
            info = json.loads(metadata)
            if info.get("ref") != KAGGLE_REF or not any(
                version.get("versionNumber") == 2 for version in info.get("versions", [])
            ):
                raise DatasetUnavailable(
                    "Kaggle publisher/version not verified; acquisition pending"
                )
            raw = _fetch(config["download_url"])
            save("source-v2.zip", raw, config["download_url"], config["archive_sha256"])
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for item in (config, config["license"], config["readme"]):
                    save(
                        item["filename"],
                        archive.read(item["archive_member"]),
                        config["download_url"] + "#" + item["archive_member"],
                        item["sha256"],
                    )
        else:
            for item, url in (
                (config, config["download_url"]),
                (config["license"], config["license"]["evidence_url"]),
                (config["readme"], config["readme"]["url"]),
            ):
                save(item["filename"], _fetch(url), url, item["sha256"])
        imported = import_dataset(
            source_id, folder / config["filename"], folder / config["license"]["filename"]
        )
        acquisition["status"] = "verified-local-data"
        acquisition["row_count"] = imported["manifest"]["row_count"]
        acquisition["license"] = config["license"]
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        acquisition["reason"] = str(exc)
        raise DatasetUnavailable(str(exc)) from exc
    finally:
        with acquisition_path.open("x", encoding="utf-8") as handle:
            json.dump(acquisition, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return acquisition
