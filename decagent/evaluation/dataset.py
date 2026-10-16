"""Validate provenance, fixed acceptance evidence and independently human-labeled data."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from decagent.decisions.calibration import (
    CalibrationPrediction,
    HumanDataset,
    calibrate,
)
from decagent.decisions.contracts import QUESTIONS, ChoiceAnswer, NoulAnswer, digest

from .contracts import sha256


def question_class(question):
    if question not in QUESTIONS:
        raise ValueError("Unknown question")
    return "memory_read" if question.startswith("memory_r") else question


def load_human_bundle(path):
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(bundle) != {"format_version", "status", "dataset", "inputs"}:
        raise ValueError("Invalid human labeling bundle")
    if bundle["format_version"] != "1":
        raise ValueError("Unknown human labeling format")
    dataset = HumanDataset.from_dict(bundle["dataset"])
    inputs = bundle["inputs"]
    if set(inputs) != {s.sample_id for s in dataset.samples}:
        raise ValueError("Missing or extra sample inputs")
    for sample in dataset.samples:
        evidence = inputs[sample.sample_id]
        if not isinstance(evidence, dict) or digest(evidence) != sample.input_hash:
            raise ValueError("Sample input hash mismatch")
        if set(evidence) - set(QUESTIONS[sample.question_id].state_fields):
            raise ValueError("Evidence has unrelated question fields")
    if bundle["status"] == "human_labeled" and not dataset.has_complete_human_labels:
        raise ValueError("Unlabeled proposal cannot claim ground truth")
    if bundle["status"] not in {"proposal_unlabeled", "human_labeled", "synthetic_fixture"}:
        raise ValueError("Unknown labeling status")
    return dataset, inputs


def https_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source URLs must be HTTPS and credential-free")


@dataclass(frozen=True)
class BugTask:
    task_id: str
    repository_url: str
    license_id: str
    license_url: str
    base_sha: str
    reference_sha: str
    issue_url: str
    problem_statement: str
    acceptance_sha256: str
    acceptance_nodes: tuple[str, ...]
    dependencies: tuple[str, ...]
    status: str
    evidence: dict | None = None

    def __post_init__(self):
        for url in (self.repository_url, self.license_url, self.issue_url):
            https_url(url)
        if not self.license_id or not self.task_id or not self.problem_statement:
            raise ValueError("Task requires license, identifier and real issue description")
        if (
            any(
                not re.fullmatch(r"[0-9a-f]{40}", sha)
                for sha in (self.base_sha, self.reference_sha)
            )
            or self.base_sha == self.reference_sha
        ):
            raise ValueError("Base and reference must be distinct fixed commit SHAs")
        if self.base_sha not in self.license_url:
            raise ValueError("License must be tied to the fixed base commit")
        sha256(self.acceptance_sha256)
        if not self.acceptance_nodes or any(
            ".." in node or node.startswith("/") for node in self.acceptance_nodes
        ):
            raise ValueError("Fixed acceptance requires safe test nodes")
        if self.status not in {"unverified", "verified", "environment_failed", "reference_failed"}:
            raise ValueError("Unknown bug verification status")
        object.__setattr__(self, "acceptance_nodes", tuple(self.acceptance_nodes))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if self.status == "verified" and not self.evidence:
            raise ValueError("Verified tasks require actual execution evidence")


@dataclass(frozen=True)
class BugDataset:
    dataset_version: str
    tasks: tuple[BugTask, ...]
    source: dict


def load_bug_dataset(path, *, evidence_root):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value["format_version"] != "1" or not value["dataset_version"]:
        raise ValueError("Unknown bug dataset version")
    source = value["source"]
    https_url(source["url"])
    if not source["license_id"] or not source["permission_note"]:
        raise ValueError("Dataset provenance requires license and permission")
    tasks = tuple(BugTask(**row) for row in value["tasks"])
    if len({t.task_id for t in tasks}) != len(tasks):
        raise ValueError("Duplicate bug identifiers")
    root = Path(evidence_root).resolve()
    for task in tasks:
        acceptance = root / "acceptance" / task.task_id / "test_bug.py"
        if hashlib.sha256(acceptance.read_bytes()).hexdigest() != task.acceptance_sha256:
            raise ValueError("Fixed acceptance was modified")
        if task.status != "verified":
            continue
        evidence = task.evidence
        if (
            evidence["base"]["exit_code"] != 1
            or evidence["reference"]["exit_code"] != 0
            or evidence["base"]["failed"] < 1
            or evidence["reference"]["passed"] < 1
            or evidence["reference"]["failed"] != 0
            or evidence["base"]["collected"] != evidence["reference"]["collected"]
            or evidence["base"]["collected"] < 1
        ):
            raise ValueError("Verified task requires related base failure and reference passes")
        for stage in ("base", "reference"):
            record = evidence[stage]
            if record["skipped"] or record["xfail"] or record["errors"]:
                raise ValueError("Skipped, xfailed or environment errors cannot validate a bug")
            log = (root / record["log_path"]).resolve()
            if not log.is_relative_to(root):
                raise ValueError("Evidence path escapes benchmark directory")
            if hashlib.sha256(log.read_bytes()).hexdigest() != record["log_sha256"]:
                raise ValueError("Execution evidence hash mismatch")
        if not evidence["image_digest"].startswith("sha256:"):
            raise ValueError("Verification requires a fixed container image digest")
    return BugDataset(value["dataset_version"], tasks, source)


def calibrate_fixture(path):
    """Synthetic labeled fixtures only test the fitting algorithm, never empirical accuracy."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value["status"] != "synthetic_fixture":
        raise ValueError("Fixture must explicitly declare synthetic labels")
    dataset = HumanDataset.from_dict(value["dataset"])
    predictions = []
    for row in value["predictions"]:
        data = row["answer"]
        answer = (
            ChoiceAnswer(data["choice"], data["probabilities"], data["confidence"])
            if (data["type"] == "choice")
            else NoulAnswer(data["noul"])
        )
        predictions.append(
            CalibrationPrediction(
                row["sample_id"], row["actual_model"], row["question_version"], answer
            )
        )
    return tuple(
        calibrate(
            dataset, q, value["actual_model"], predictions, policy_version=value["policy_version"]
        )
        for q in dict.fromkeys(sample.question_id for sample in dataset.samples)
    )
