"""Private adapter to the pinned EnterpriseOps-Gym VerifierEngine."""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import sys
import tarfile
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import perf_counter
from zipfile import ZipFile, is_zipfile

from decagent.decisions.contracts import canonical_json, digest
from decagent.evaluation.business.routes import TaskScore

_IMPORT_LOCK = RLock()
_PINNED_ROOT = (
    Path(__file__).resolve().parents[3]
    / "evals"
    / "benchmarks"
    / "vendor_cache"
    / "enterpriseops-gym"
)
_MAX_TABLES = 128
_MAX_ROWS = _MAX_TABLES * 10_000
_MAX_TOOL_CALLS = 512
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class EnterpriseScoringError(RuntimeError):
    """The official scorer could not produce a valid private task verdict."""


class BudgetedEnterpriseJudge:
    """Explicit bridge to an injected judge that reports measured usage."""

    _USAGE_FIELDS = {
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "seconds",
    }

    def __init__(self, llm, usage_snapshot):
        if not callable(getattr(llm, "ainvoke", None)) or not callable(usage_snapshot):
            raise TypeError("A judge LLM and usage snapshot callback are required")
        self.llm = self
        self._llm = llm
        self._usage_snapshot = usage_snapshot
        self.dispatches = 0
        self._blocked = False

    async def ainvoke(self, messages):
        # The evaluator supplies an already authorized client. Record each
        # invocation independently so partial accounting cannot pass the gate.
        if self._blocked:
            raise EnterpriseScoringError("Response-check judge is blocked by unknown usage")
        before = self.usage_snapshot()
        self.dispatches += 1
        try:
            return await self._llm.ainvoke(messages)
        finally:
            try:
                after = self.usage_snapshot()
                EnterpriseOfficialScorer._judge_usage_delta(before, after, expected_requests=1)
            except Exception as exc:
                self._blocked = True
                raise EnterpriseScoringError("Response-check judge usage is unknown") from exc

    def usage_snapshot(self):
        value = self._usage_snapshot()
        if type(value) is not dict or set(value) != self._USAGE_FIELDS:
            raise EnterpriseScoringError("Response-check judge usage is invalid")
        if any(
            type(value[name]) is not int or value[name] < 0
            for name in ("requests", "prompt_tokens", "completion_tokens")
        ):
            raise EnterpriseScoringError("Response-check judge token usage is invalid")
        try:
            cost = (
                Decimal(str(value["cost_usd"]))
                if value["cost_usd"] is not None
                else None
            )
        except (InvalidOperation, ValueError) as exc:
            raise EnterpriseScoringError("Response-check judge cost is invalid") from exc
        seconds = value["seconds"]
        if (
            (
                cost is not None
                and (not cost.is_finite() or cost < 0)
            )
            or type(seconds) not in {int, float}
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise EnterpriseScoringError("Response-check judge usage is invalid")
        return {
            "requests": value["requests"],
            "prompt_tokens": value["prompt_tokens"],
            "completion_tokens": value["completion_tokens"],
            "cost_usd": cost,
            "seconds": float(seconds),
        }


def bounded_state_fingerprint(value):
    """Validate and retain only bounded, non-row database fingerprint fields."""
    if type(value) is not dict or set(value) != {"digest", "table_count", "row_count"}:
        raise EnterpriseScoringError("Private database fingerprint is invalid")
    fingerprint = value["digest"]
    tables, rows = value["table_count"], value["row_count"]
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 64
        or any(character not in _HEX for character in fingerprint)
        or type(tables) is not int
        or not 1 <= tables <= _MAX_TABLES
        or type(rows) is not int
        or not 0 <= rows <= _MAX_ROWS
    ):
        raise EnterpriseScoringError("Private database fingerprint is outside its bounds")
    return {"digest": fingerprint, "table_count": tables, "row_count": rows}


def _load_pinned_verifier_types():
    root = _PINNED_ROOT.resolve()
    verifier_path = (root / "benchmark" / "verifier.py").resolve()
    models_path = (root / "benchmark" / "models.py").resolve()
    if not verifier_path.is_relative_to(root) or not models_path.is_relative_to(root):
        raise EnterpriseScoringError("Pinned verifier source path is invalid")
    if not verifier_path.is_file() or not models_path.is_file():
        raise EnterpriseScoringError("Pinned EnterpriseOps verifier source is unavailable")
    _verify_pinned_source(root)

    with _IMPORT_LOCK:
        root_text = str(root)
        inserted = root_text not in sys.path
        if inserted:
            sys.path.insert(0, root_text)
        try:
            verifier_module = importlib.import_module("benchmark.verifier")
            models_module = importlib.import_module("benchmark.models")
        except Exception as exc:
            raise EnterpriseScoringError("Pinned EnterpriseOps verifier could not load") from exc
        finally:
            if inserted and root_text in sys.path:
                sys.path.remove(root_text)
    if (
        Path(verifier_module.__file__).resolve() != verifier_path
        or Path(models_module.__file__).resolve() != models_path
    ):
        raise EnterpriseScoringError("A non-pinned EnterpriseOps verifier was loaded")
    return verifier_module.VerifierEngine, models_module.VerifierConfig


def _verify_pinned_source(root):
    """Verify executable benchmark sources against the hashed upstream archive."""
    try:
        manifest_path = root.parent.parent / "manifests/source_manifest.json"
        project_root = manifest_path.parents[3].resolve()
        record = json.loads(manifest_path.read_text(encoding="utf-8"))["enterpriseops_gym"][
            "source_repository"
        ]["archive"]
        archive = (project_root / record["path"]).resolve()
        if not archive.is_relative_to(project_root) or archive.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Invalid source archive")
        if sha256(archive.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Source archive checksum mismatch")
        entries = {}
        if is_zipfile(archive):
            with ZipFile(archive) as bundle:
                for entry in bundle.infolist():
                    relative = entry.filename.split("/", 1)[-1]
                    if not relative.startswith("benchmark/") or not relative.endswith(".py"):
                        continue
                    if relative in entries or entry.file_size > 2 * 1024 * 1024:
                        raise ValueError("Invalid source entry")
                    entries[relative] = bundle.read(entry)
        else:
            with tarfile.open(archive, "r:*") as bundle:
                for entry in bundle:
                    relative = entry.name.split("/", 1)[-1]
                    if not relative.startswith("benchmark/") or not relative.endswith(".py"):
                        continue
                    if not entry.isfile() or relative in entries or entry.size > 2 * 1024 * 1024:
                        raise ValueError("Invalid source entry")
                    with bundle.extractfile(entry) as source:
                        entries[relative] = source.read()
        actual = {path.relative_to(root).as_posix() for path in (root / "benchmark").rglob("*.py")}
        if not entries or set(entries) != actual:
            raise ValueError("Source file set changed")
        for name, expected in entries.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or path.read_bytes() != expected:
                raise ValueError("Source file content changed")
    except Exception as exc:
        raise EnterpriseScoringError("Pinned EnterpriseOps source verification failed") from exc


def _run_async(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise EnterpriseScoringError("Private verifier cannot run inside an active event loop")


class EnterpriseOfficialScorer:
    """Execute unchanged official configs and retain only private audit digests.

    ``judge_client`` is an evaluator-injected, budget-accounted dependency for
    response_check verifiers. This class never constructs a model provider;
    the original verifier can invoke only that explicitly supplied judge.
    Tests may inject the upstream engine/config types without importing any
    benchmark or model runtime.
    """

    def __init__(
        self,
        task_spec,
        *,
        judge_client=None,
        engine_factory=None,
        verifier_config_factory=None,
    ):
        self._spec = task_spec
        self._judge_client = judge_client
        self._engine_factory = engine_factory
        self._verifier_config_factory = verifier_config_factory
        self._private_audit_evidence = None
        self._observed_judge_usage = None

    @property
    def requires_judge(self):
        configs = self._spec.verifiers
        return any(
            type(config) is dict and config.get("verifier_type") == "response_check"
            for config in configs
        ) if type(configs) in {tuple, list} else False

    @property
    def judge_client_configured(self):
        return self._judge_client is not None

    def configure_judge_client(self, judge_client):
        if not self.requires_judge:
            raise EnterpriseScoringError("This task has no response-check judge")
        if not isinstance(judge_client, BudgetedEnterpriseJudge):
            raise TypeError("Response-check judge must use the measured budget adapter")
        if self._judge_client is not None:
            raise EnterpriseScoringError("Response-check judge configuration is immutable")
        self._judge_client = judge_client

    @property
    def private_audit_evidence(self):
        return deepcopy(self._private_audit_evidence)

    @property
    def observed_judge_usage(self):
        return deepcopy(self._observed_judge_usage)

    def _verifier_runtime(self, client):
        if self._engine_factory is None or self._verifier_config_factory is None:
            engine_type, config_type = _load_pinned_verifier_types()
            engine_factory = self._engine_factory or (
                lambda clients, judge: engine_type(clients, llm_client=judge)
            )
            config_factory = self._verifier_config_factory or config_type
        else:
            engine_factory = self._engine_factory
            config_factory = self._verifier_config_factory
        return (
            engine_factory({self._spec.server_name: client}, self._judge_client),
            config_factory,
        )

    @staticmethod
    def _judge_usage_delta(before, after, *, expected_requests):
        delta = {
            name: after[name] - before[name]
            for name in ("requests", "prompt_tokens", "completion_tokens")
        }
        if any(value < 0 for value in delta.values()):
            raise EnterpriseScoringError("Response-check judge usage moved backwards")
        cost = (
            after["cost_usd"] - before["cost_usd"]
            if before["cost_usd"] is not None and after["cost_usd"] is not None
            else None
        )
        seconds = after["seconds"] - before["seconds"]
        if (
            (cost is not None and cost < 0)
            or seconds < 0
            or expected_requests < 1
            or delta["requests"] != expected_requests
        ):
            raise EnterpriseScoringError("Response-check judge usage was not measured")
        return {
            "requests": delta["requests"],
            "tokens": delta["prompt_tokens"] + delta["completion_tokens"],
            "cost_usd": cost,
            "seconds": seconds,
        }

    def _write_audit(
        self,
        *,
        initial_state_fingerprint,
        post_state_fingerprint,
        verifier_count,
        passed_count,
        result_digest,
        model_response_digest,
        scoring_status,
        failure_type=None,
    ):
        try:
            initial = bounded_state_fingerprint(initial_state_fingerprint)
        except EnterpriseScoringError:
            initial = None
        try:
            post = bounded_state_fingerprint(post_state_fingerprint)
        except EnterpriseScoringError:
            post = None
        self._private_audit_evidence = {
            "version": "enterpriseops-private-score-v1",
            "initial_state_fingerprint": initial,
            "post_state_fingerprint": post,
            "scoring_status": scoring_status,
            "verifier_count": verifier_count,
            "passed_count": passed_count,
            "verifier_results_digest": result_digest,
            "model_response_digest": model_response_digest,
            "forbidden_side_effects": "not_measured",
            "failure_type": failure_type,
            "judge_usage": {
                **deepcopy(self._observed_judge_usage),
                "cost_usd": (
                    str(self._observed_judge_usage["cost_usd"])
                    if self._observed_judge_usage["cost_usd"] is not None
                    else None
                ),
            },
        }

    def score_with_model_response(
        self,
        environment,
        model_response,
        *,
        initial_state_fingerprint,
    ):
        self._private_audit_evidence = None
        self._observed_judge_usage = {
            "requests": 0,
            "tokens": 0,
            "cost_usd": Decimal("0"),
            "seconds": 0.0,
            "complete": True,
        }
        configs = self._spec.verifiers
        verifier_count = len(configs) if type(configs) in {tuple, list} else 0
        response_digest = None
        passed_count = 0
        result_digest = None
        post_fingerprint = None
        judge_usage = {"requests": 0, "tokens": 0, "cost_usd": Decimal("0"), "seconds": 0.0}
        judge_usage_before = None
        judge_dispatches_before = (
            self._judge_client.dispatches
            if isinstance(self._judge_client, BudgetedEnterpriseJudge)
            else 0
        )
        judge_started = perf_counter()
        try:
            initial = bounded_state_fingerprint(initial_state_fingerprint)
            if (
                type(model_response) is not dict
                or set(model_response) != {"content", "tool_calls"}
                or type(model_response["content"]) is not str
                or type(model_response["tool_calls"]) is not list
                or len(model_response["tool_calls"]) > _MAX_TOOL_CALLS
                or len(canonical_json(model_response).encode("utf-8")) > _MAX_RESPONSE_BYTES
            ):
                raise EnterpriseScoringError("Official model response projection is invalid")
            response_digest = digest(model_response)
            if not configs or type(configs) not in {tuple, list}:
                raise EnterpriseScoringError("Pinned task has no official verifier configs")
            response_check = any(
                type(config) is dict and config.get("verifier_type") == "response_check"
                for config in configs
            )
            if response_check and not model_response["content"].strip():
                raise EnterpriseScoringError(
                    "Response check requires an actual final response from the route"
                )
            if response_check and self._judge_client is None:
                raise EnterpriseScoringError(
                    "Response check requires an injected budget-accounted judge"
                )
            if response_check and not isinstance(self._judge_client, BudgetedEnterpriseJudge):
                raise EnterpriseScoringError(
                    "Response check requires an injected budget-accounted judge"
                )
            judge_usage_before = self._judge_client.usage_snapshot() if response_check else None
            judge_dispatches_before = self._judge_client.dispatches if response_check else 0
            if any(type(config) is not dict for config in configs):
                raise EnterpriseScoringError("Pinned verifier config is invalid")

            hook = environment.scorer_hook()
            engine, config_factory = self._verifier_runtime(hook.client)
            original_configs = [config_factory(**deepcopy(config)) for config in configs]
            results = _run_async(hook.run(engine, original_configs, deepcopy(model_response)))
            if response_check:
                judge_usage = self._judge_usage_delta(
                    judge_usage_before,
                    self._judge_client.usage_snapshot(),
                    expected_requests=self._judge_client.dispatches - judge_dispatches_before,
                )
                self._observed_judge_usage = {**judge_usage, "complete": True}
            if type(results) is not list or len(results) != len(original_configs):
                raise EnterpriseScoringError("Original verifier returned an invalid result list")
            if any(
                type(result) is not dict
                or type(result.get("passed")) is not bool
                or "error" in result
                for result in results
            ):
                raise EnterpriseScoringError("Original verifier returned an invalid result")

            passed_count = sum(result["passed"] for result in results)
            result_digest = digest(results)
            post_fingerprint = bounded_state_fingerprint(environment.state_fingerprint())
            success = all(result["passed"] for result in results)
            self._write_audit(
                initial_state_fingerprint=initial,
                post_state_fingerprint=post_fingerprint,
                verifier_count=len(results),
                passed_count=passed_count,
                result_digest=result_digest,
                model_response_digest=response_digest,
                scoring_status="scored",
            )
            return TaskScore(
                success=success,
                forbidden_side_effects=None,
                benchmark_score=float(success),
                official=True,
                judge_cost_usd=judge_usage["cost_usd"],
                judge_requests=judge_usage["requests"],
                judge_tokens=judge_usage["tokens"],
                judge_seconds=judge_usage["seconds"],
            )
        except Exception as exc:
            if judge_usage_before is not None:
                dispatches = self._judge_client.dispatches - judge_dispatches_before
                if dispatches:
                    try:
                        measured = self._judge_usage_delta(
                            judge_usage_before,
                            self._judge_client.usage_snapshot(),
                            expected_requests=dispatches,
                        )
                        self._observed_judge_usage = {**measured, "complete": True}
                    except Exception:
                        self._observed_judge_usage = {
                            "requests": dispatches,
                            "tokens": None,
                            "cost_usd": None,
                            "seconds": max(0.0, perf_counter() - judge_started),
                            "complete": False,
                        }
            if post_fingerprint is None:
                try:
                    post_fingerprint = bounded_state_fingerprint(environment.state_fingerprint())
                except Exception:
                    post_fingerprint = None
            if self._private_audit_evidence is None:
                self._write_audit(
                    initial_state_fingerprint=initial_state_fingerprint,
                    post_state_fingerprint=post_fingerprint,
                    verifier_count=verifier_count,
                    passed_count=passed_count,
                    result_digest=result_digest,
                    model_response_digest=response_digest,
                    scoring_status="scoring_failure",
                    failure_type=type(exc).__name__,
                )
            if isinstance(exc, EnterpriseScoringError):
                raise
            raise EnterpriseScoringError("Original EnterpriseOps scoring failed") from exc
