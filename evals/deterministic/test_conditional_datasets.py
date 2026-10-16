"""Offline data plumbing tests; fixtures do not assert model performance."""

import copy
import importlib
import json
import subprocess
import sys

import pytest


def module():
    return importlib.import_module("decagent.evaluation.business.conditional.datasets")


def record(identifier, text, label="intent", split=None):
    return {"id": identifier, "utterance": text, "label": label, "official_split": split}


def test_sources_pin_distinct_origins_versions_hashes_and_local_license_evidence():
    data = module()
    bitext = data.source_config("kaggle-bitext")
    clinc = data.source_config("clinc150")
    assert bitext["source_url"].startswith("https://www.kaggle.com/")
    assert bitext["version"] == "2"
    assert bitext["license"]["id"] == "CDLA-Sharing-1.0"
    assert "20000-Utterances" in bitext["archive_member"]
    assert clinc["version"] == "828f8093932c8fe6ca7936c3d2e52903b1c523de"
    assert clinc["license"]["id"] == "CC-BY-3.0"
    for config in (bitext, clinc):
        assert len(config["sha256"]) == len(config["license"]["sha256"]) == 64
        assert config["redistribute_raw"] is False
    bitext["license"]["id"] = "modified"
    assert data.source_config("kaggle-bitext")["license"]["id"] == "CDLA-Sharing-1.0"
    with pytest.raises(ValueError, match="source"):
        data.source_config("huggingface-bitext")


def test_import_fails_explicitly_when_data_license_or_integrity_missing(tmp_path):
    data = module()
    missing = tmp_path / "missing"
    with pytest.raises(data.DatasetUnavailable, match="data.*missing"):
        data.import_dataset("clinc150", missing, missing)
    raw = tmp_path / "data.json"
    raw.write_text("{}", encoding="utf-8")
    with pytest.raises(data.DatasetUnavailable, match="license.*missing"):
        data.import_dataset("clinc150", raw, missing)
    license_file = tmp_path / "LICENSE"
    license_file.write_text("Unclear license", encoding="utf-8")
    with pytest.raises(data.DatasetUnavailable, match="license.*SHA256"):
        data.import_dataset("clinc150", raw, license_file)
    with pytest.raises(data.DatasetUnavailable, match="license.*not verified"):
        data.verify_license({"license": {"verified": False}}, license_file)


def test_parsers_preserve_official_partitions_and_discard_answer_metadata():
    data = module()
    rows = data.parse_bitext(
        b"flags,utterance,category,intent,response\nB,Please help,ACCOUNT,help,SECRET\n"
    )
    assert rows[0]["label"] == "help"
    assert set(rows[0]) == {"id", "utterance", "label", "official_split"}
    payload = {
        key: [[key + " user", "oos" if key.startswith("oos") else "help"]]
        for key in ("train", "val", "test", "oos_train", "oos_val", "oos_test")
    }
    parsed = data.parse_clinc(json.dumps(payload).encode())
    assert [row["official_split"] for row in parsed] == [
        "development",
        "validation",
        "test",
        "development",
        "validation",
        "test",
    ]
    with pytest.raises(ValueError, match="official split"):
        data.parse_clinc(b'{"train": []}')


def test_model_input_uses_explicit_allowlist_even_with_adversarial_fields():
    row = {
        **record("id", "Raw request", "secret"),
        "response": "ANSWER",
        "category": "ACCOUNT",
        "flags": "B",
        "answer": "label",
        "options": ["leak"],
    }
    assert module().model_input(row) == {"utterance": "Raw request"}
    assert module().model_input(row, options=["legal-a", "legal-b"]) == {
        "utterance": "Raw request",
        "options": ["legal-a", "legal-b"],
    }


def test_text_only_groups_cover_normalization_near_duplicates_and_template_slots():
    rows = [
        record("a", "Track my order 1234!", "one"),
        record("b", "  TRACK my order 9999 ", "two"),
        record("c", "please explain how i can update my billing address today"),
        record("d", "please explain how i can update my billing address tomorrow"),
        record("e", "send receipt to {{email}}"),
        record("f", "send receipt to person@example.com"),
        record("g", "unrelated question"),
    ]
    grouped = module().group_records(rows)
    groups = {row["id"]: row["group_id"] for row in grouped}
    assert groups["a"] == groups["b"]
    assert groups["c"] == groups["d"]
    assert groups["e"] == groups["f"]
    assert groups["g"] != groups["a"]
    changed = copy.deepcopy(rows)
    for row in changed:
        row["label"] = "changed"
    assert {r["id"]: r["group_id"] for r in module().group_records(changed)} == groups
    assert {r["id"]: r["group_id"] for r in module().group_records(rows[::-1])} == groups


def test_bitext_split_is_group_disjoint_seeded_approximately_stratified():
    rows = [
        record(f"{label}-{index}", f"unique {label} topic {chr(0x4E00 + index)}", label)
        for label in ("alpha", "beta", "gamma")
        for index in range(50)
    ]
    rows += [record("duplicate", rows[0]["utterance"], "alpha")]
    data = module()
    prepared = data.prepare_records(rows, kind="bitext", validation_size=18, test_size=24)
    again = data.prepare_records(rows[::-1], kind="bitext", validation_size=18, test_size=24)
    assert prepared == again
    assert prepared["manifest"]["seed"] == 20261015
    split_groups = [
        {row["group_id"] for row in prepared["splits"][split]}
        for split in ("development", "validation", "test")
    ]
    assert all(not a & b for index, a in enumerate(split_groups) for b in split_groups[index + 1 :])
    for split, size in (("validation", 18), ("test", 24)):
        assert len(prepared["subsets"][split]) == size
        counts = [
            sum(row["label"] == label for row in prepared["subsets"][split])
            for label in ("alpha", "beta", "gamma")
        ]
        assert max(counts) - min(counts) <= 2
    assert prepared["manifest"]["deduplicated_rows"] == 1
    assert prepared["shortfalls"] == []


def test_clinc_keeps_official_splits_and_excludes_cross_split_groups():
    rows = [
        record("train-a", "same text", split="development"),
        record("val-a", "SAME TEXT!", split="validation"),
        record("test-a", "independent utterance", split="test"),
    ]
    prepared = module().prepare_records(
        rows, kind="clinc", validation_size=4, test_size=4, validation_oos=1, test_oos=1
    )
    assert prepared["splits"]["development"] == prepared["splits"]["validation"] == []
    assert prepared["splits"]["test"][0]["id"] == "test-a"
    assert {row["id"] for row in prepared["excluded"]} == {"train-a", "val-a"}
    assert all(row["reason"] == "cross_official_split_group" for row in prepared["excluded"])
    assert prepared["shortfalls"]


def test_clinc_stratifies_within_split_keeps_oos_budget_and_reports_shortfall():
    rows = [
        record(
            f"{split}-{label}-{index}",
            f"{split} topic {label} symbol {chr(0x4E00 + index)}",
            label,
            split,
        )
        for split in ("development", "validation", "test")
        for label in ("alpha", "beta", "oos")
        for index in range(30)
    ]
    prepared = module().prepare_records(
        rows, kind="clinc", validation_size=20, test_size=40, validation_oos=5, test_oos=10
    )
    for split, size, oos in (("validation", 20, 5), ("test", 40, 10)):
        subset = prepared["subsets"][split]
        assert len(subset) == size
        assert sum(row["label"] == "oos" for row in subset) == oos
        assert all(row["official_split"] == split for row in subset)
    short = module().prepare_records(rows[:2], kind="clinc")
    assert len(short["subsets"]["validation"]) == len(short["subsets"]["test"]) == 0
    assert {row["split"] for row in short["shortfalls"]} == {"validation", "test"}


def test_freeze_manifest_binds_sample_lists_and_refuses_overwrite(tmp_path):
    data = module()
    prepared = data.prepare_records([record("one", "request")], kind="bitext")
    path = tmp_path / "frozen.json"
    data.freeze_prepared(prepared, path)
    assert data.load_prepared(path) == prepared
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["subsets"]["test"].append(record("injected", "tampered"))
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        data.load_prepared(path)
    with pytest.raises(FileExistsError):
        data.freeze_prepared(prepared, path)


def test_dataset_cli_missing_data_exits_nonzero_without_model_or_database(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "decagent.evaluation.business.conditional.dataset_cli",
            "prepare",
            "--source",
            "clinc150",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "data" in result.stderr and "missing" in result.stderr
    assert not list(tmp_path.rglob("*.json"))


def test_imported_fixture_records_manifest_integrity_and_label_version(tmp_path, monkeypatch):
    data = module()
    raw = b"flags,utterance,category,intent,response\nB,Fixture request,SECRET,help,SECRET\n"
    license_bytes = b"offline test license fixture"
    config = data.source_config("kaggle-bitext")
    config["sha256"] = data.sha256(raw)
    config["license"]["sha256"] = data.sha256(license_bytes)
    monkeypatch.setitem(data.SOURCES, "kaggle-bitext", config)
    raw_path, license_path = tmp_path / "data.csv", tmp_path / "LICENSE"
    raw_path.write_bytes(raw)
    license_path.write_bytes(license_bytes)
    imported = data.import_dataset("kaggle-bitext", raw_path, license_path)
    assert imported["manifest"]["row_count"] == 1
    assert imported["manifest"]["labels"] == ["help"]
    assert len(imported["manifest"]["label_version"]) == 64
    prepared = data.prepare_dataset(imported)
    assert prepared["manifest"]["source_verified"] is True
    assert prepared["manifest"]["source"]["sha256"] == data.sha256(raw)
    raw_path.write_bytes(raw + b"tampered")
    with pytest.raises(data.DatasetUnavailable, match="data SHA256"):
        data.import_dataset("kaggle-bitext", raw_path, license_path)


def test_acquisition_network_failure_persists_pending_without_claiming_a_benchmark(
    tmp_path, monkeypatch
):
    data = module()

    def unavailable(url):
        raise data.DatasetUnavailable("public source unavailable")

    monkeypatch.setattr(data, "_fetch", unavailable)
    with pytest.raises(data.DatasetUnavailable, match="unavailable"):
        data.acquire_dataset("clinc150", tmp_path)
    acquisition = tmp_path / "clinc150" / "acquisition.json"
    result = json.loads(acquisition.read_text(encoding="utf-8"))
    assert result["status"] == "pending"
    assert result["files"] == []
    assert "row_count" not in result
    with pytest.raises(FileExistsError):
        data.acquire_dataset("clinc150", tmp_path)


def test_same_text_conflicting_labels_are_excluded_and_cannot_reach_models():
    prepared = module().prepare_records(
        [record("a", "Same request", "first"), record("b", "SAME request!", "second")],
        kind="bitext",
    )
    assert all(not rows for rows in prepared["splits"].values())
    assert {row["reason"] for row in prepared["excluded"]} == {"conflicting_normalized_labels"}
    assert prepared["shortfalls"]


def test_invalid_records_and_unknown_official_partitions_fail_closed():
    data = module()
    for raw in (b"text,label\nhello,intent\n", b"utterance,intent\n,help\n"):
        with pytest.raises(ValueError):
            data.parse_bitext(raw)
    with pytest.raises(ValueError, match="official split"):
        data.prepare_records([record("id", "request")], kind="clinc")
    with pytest.raises(ValueError, match="duplicate sample IDs"):
        data.group_records([record("id", "first"), record("id", "second")])
    with pytest.raises(ValueError, match="nonnegative"):
        data.prepare_records([], kind="bitext", validation_size=-1)


def test_stratification_reports_missing_or_scarce_classes_without_cross_split_fill():
    rows = [
        record(f"{label}-{i}", f"topic {label} symbol {chr(0x4E00 + i)}", label)
        for label in ("alpha", "beta")
        for i in range(30)
    ]
    rows.append(record("rare", "very scarce separate request", "rare"))
    prepared = module().prepare_records(rows, kind="bitext", validation_size=12, test_size=12)
    notes = prepared["manifest"]["stratification_shortfalls"]
    assert any(note["label"] == "rare" and note["actual"] == 0 for note in notes)
    assert all(note["nominal_minimum"] == 4 for note in notes)
    assert prepared["shortfalls"] == []  # Total achieved; imbalance remains explicit.
