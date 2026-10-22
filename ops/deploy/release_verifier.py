"""Verify a DecAgent release before a fixed private-deployment workflow uses it.

This module intentionally uses only the Python standard library so the reviewed
copy can live in the private deployment repository without installing project
dependencies. It accepts no command, path, repository, or image override.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_REPOSITORY = "shuaibilx/DecAgent"
DEPLOY_REPOSITORY = "shuaibilx/DecAgent-deploy"
DEPLOY_REF = "refs/heads/main"
CONTROL_IMAGE = "ghcr.io/shuaibilx/decagent-control"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
REQUIRED_JOBS = frozenset({"Full CI release gate", "Publish versioned GHCR images"})
DISPATCH_FIELDS = frozenset({"source_repository", "tag", "sha", "run_id", "run_attempt", "image"})
_TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_RE = re.compile(re.escape(CONTROL_IMAGE) + r"@sha256:([0-9a-f]{64})\Z")


class ReleaseVerificationError(ValueError):
    """Raised when release identity or provenance does not match policy."""


@dataclass(frozen=True)
class ReleaseRequest:
    source_repository: str
    tag: str
    sha: str
    run_id: int
    run_attempt: int
    image: str


@dataclass(frozen=True)
class ReleaseRunEvidence:
    run_id: int
    run_attempt: int
    workflow_path: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None
    head_sha: str


@dataclass(frozen=True)
class ReleaseJobEvidence:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class ReleaseImageEvidence:
    tag: str
    repository_digests: tuple[str, ...]
    source: str | None
    revision: str | None
    version: str | None


@dataclass(frozen=True)
class ReleaseEvidence:
    tag_commit_sha: str
    main_sha: str
    comparison_status: str
    ahead_by: int
    behind_by: int
    run: ReleaseRunEvidence
    jobs: tuple[ReleaseJobEvidence, ...]
    image: ReleaseImageEvidence


@dataclass(frozen=True)
class VerifiedRelease:
    image: str
    tag: str
    sha: str
    run_id: int
    run_attempt: int


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReleaseVerificationError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseVerificationError(f"{field} must be a positive integer") from exc
    if parsed < 1 or str(parsed) != str(value):
        raise ReleaseVerificationError(f"{field} must be a positive integer")
    return parsed


def parse_dispatch_event(event: Mapping[str, Any]) -> ReleaseRequest:
    """Parse the deliberately narrow workflow_dispatch input contract."""
    inputs = event.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ReleaseVerificationError("dispatch event must contain an inputs object")
    fields = set(inputs)
    unexpected = fields - DISPATCH_FIELDS
    missing = DISPATCH_FIELDS - fields
    if unexpected:
        raise ReleaseVerificationError(
            "unexpected dispatch input: " + ", ".join(sorted(unexpected))
        )
    if missing:
        raise ReleaseVerificationError("missing dispatch input: " + ", ".join(sorted(missing)))
    return ReleaseRequest(
        source_repository=str(inputs["source_repository"]),
        tag=str(inputs["tag"]),
        sha=str(inputs["sha"]),
        run_id=_positive_integer(inputs["run_id"], "run_id"),
        run_attempt=_positive_integer(inputs["run_attempt"], "run_attempt"),
        image=str(inputs["image"]),
    )


def validate_release_request(
    request: ReleaseRequest,
    *,
    deployment_repository: str,
    deployment_ref: str,
) -> None:
    """Reject untrusted identity and syntax before any API or Docker access."""
    if deployment_repository != DEPLOY_REPOSITORY or deployment_ref != DEPLOY_REF:
        raise ReleaseVerificationError("deployment must run from private repository main")
    if request.source_repository != SOURCE_REPOSITORY:
        raise ReleaseVerificationError("source repository is not the fixed DecAgent repository")
    if not _TAG_RE.fullmatch(request.tag):
        raise ReleaseVerificationError("release tag must be vMAJOR.MINOR.PATCH")
    if not _SHA_RE.fullmatch(request.sha):
        raise ReleaseVerificationError("release SHA must be 40 lowercase hexadecimal characters")
    if _positive_integer(request.run_id, "run_id") != request.run_id:
        raise ReleaseVerificationError("run_id must be a positive integer")
    if _positive_integer(request.run_attempt, "run_attempt") != request.run_attempt:
        raise ReleaseVerificationError("run_attempt must be a positive integer")
    if not _IMAGE_RE.fullmatch(request.image):
        raise ReleaseVerificationError("image must be the fixed control image at a SHA-256 digest")


def validate_release(
    request: ReleaseRequest,
    evidence: ReleaseEvidence,
    *,
    deployment_repository: str,
    deployment_ref: str,
) -> VerifiedRelease:
    """Validate all provenance and image evidence before returning a deployable ref."""
    validate_release_request(
        request,
        deployment_repository=deployment_repository,
        deployment_ref=deployment_ref,
    )
    if evidence.tag_commit_sha != request.sha or not _SHA_RE.fullmatch(evidence.tag_commit_sha):
        raise ReleaseVerificationError("release tag does not resolve to the requested SHA")
    if not _SHA_RE.fullmatch(evidence.main_sha):
        raise ReleaseVerificationError("main branch did not resolve to a valid SHA")
    if evidence.comparison_status not in {"ahead", "identical"} or evidence.behind_by != 0:
        raise ReleaseVerificationError("release SHA is not an ancestor of main")
    if evidence.ahead_by < 0 or evidence.behind_by < 0:
        raise ReleaseVerificationError("GitHub comparison returned invalid commit counts")
    if (evidence.comparison_status == "identical" and evidence.ahead_by != 0) or (
        evidence.comparison_status == "ahead" and evidence.ahead_by < 1
    ):
        raise ReleaseVerificationError("GitHub comparison status and counts disagree")

    run = evidence.run
    if (
        run.run_id != request.run_id
        or run.run_attempt != request.run_attempt
        or run.workflow_path != RELEASE_WORKFLOW
        or run.head_branch != request.tag
        or run.event != "push"
        or run.status != "completed"
        or run.conclusion != "success"
        or run.head_sha != request.sha
    ):
        raise ReleaseVerificationError("requested release run or attempt is not successful")

    for required_name in REQUIRED_JOBS:
        matching = [job for job in evidence.jobs if job.name == required_name]
        if (
            len(matching) != 1
            or matching[0].status != "completed"
            or matching[0].conclusion != "success"
        ):
            raise ReleaseVerificationError(f"required release job did not succeed: {required_name}")

    image = evidence.image
    expected_tag = f"{CONTROL_IMAGE}:sha-{request.sha}"
    if image.tag != expected_tag:
        raise ReleaseVerificationError("image inspection did not use the fixed SHA tag")
    if request.image not in image.repository_digests:
        raise ReleaseVerificationError("SHA image tag does not resolve to the requested digest")
    if (
        image.source != f"https://github.com/{SOURCE_REPOSITORY}"
        or image.revision != request.sha
        or image.version != request.tag
    ):
        raise ReleaseVerificationError("container OCI labels do not match release provenance")

    return VerifiedRelease(
        image=request.image,
        tag=request.tag,
        sha=request.sha,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
    )


class GitHubEvidenceClient:
    """Read release provenance from the fixed public repository's REST API."""

    def __init__(self, token: str, *, opener=urllib.request.urlopen):
        if not token:
            raise ReleaseVerificationError("DECAGENT_PUBLIC_READ_TOKEN is required")
        self._token = token
        self._opener = opener
        self._base = f"https://api.github.com/repos/{SOURCE_REPOSITORY}"

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "DecAgent-private-release-verifier",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ReleaseVerificationError(
                f"GitHub API request failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReleaseVerificationError("GitHub API request could not be completed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError("GitHub API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ReleaseVerificationError("GitHub API returned an unexpected response")
        return payload

    def tag_commit_sha(self, tag: str) -> str:
        encoded_tag = urllib.parse.quote(tag, safe="")
        payload = self._get(f"/git/ref/tags/{encoded_tag}")
        target = payload.get("object")
        for _ in range(6):
            if not isinstance(target, dict):
                break
            object_type = target.get("type")
            object_sha = target.get("sha")
            if not isinstance(object_sha, str) or not _SHA_RE.fullmatch(object_sha):
                break
            if object_type == "commit":
                return object_sha
            if object_type != "tag":
                break
            target = self._get(f"/git/tags/{object_sha}").get("object")
        raise ReleaseVerificationError("release tag does not resolve to a commit")

    def collect(self, request: ReleaseRequest, image: ReleaseImageEvidence) -> ReleaseEvidence:
        tag_sha = self.tag_commit_sha(request.tag)
        main_ref = self._get("/git/ref/heads/main").get("object")
        main_sha = main_ref.get("sha") if isinstance(main_ref, dict) else None
        if not isinstance(main_sha, str) or not _SHA_RE.fullmatch(main_sha):
            raise ReleaseVerificationError("GitHub main ref did not resolve to a commit")

        comparison = self._get(f"/compare/{tag_sha}...{main_sha}")
        run = self._get(f"/actions/runs/{request.run_id}/attempts/{request.run_attempt}")
        jobs_payload = self._get(
            f"/actions/runs/{request.run_id}/attempts/{request.run_attempt}/jobs?per_page=100"
        )
        raw_jobs = jobs_payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise ReleaseVerificationError("GitHub Actions job list is invalid")

        try:
            run_evidence = ReleaseRunEvidence(
                run_id=int(run["id"]),
                run_attempt=int(run["run_attempt"]),
                workflow_path=str(run["path"]),
                head_branch=str(run["head_branch"]),
                event=str(run["event"]),
                status=str(run["status"]),
                conclusion=run.get("conclusion"),
                head_sha=str(run["head_sha"]),
            )
            jobs = tuple(
                ReleaseJobEvidence(
                    name=str(job["name"]),
                    status=str(job["status"]),
                    conclusion=job.get("conclusion"),
                )
                for job in raw_jobs
                if isinstance(job, dict)
            )
            ahead_by = int(comparison["ahead_by"])
            behind_by = int(comparison["behind_by"])
            comparison_status = str(comparison["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseVerificationError("GitHub release evidence is incomplete") from exc

        return ReleaseEvidence(
            tag_commit_sha=tag_sha,
            main_sha=main_sha,
            comparison_status=comparison_status,
            ahead_by=ahead_by,
            behind_by=behind_by,
            run=run_evidence,
            jobs=jobs,
            image=image,
        )


def inspect_published_image(request: ReleaseRequest) -> ReleaseImageEvidence:
    """Pull and inspect only the fixed SHA tag derived from a validated request."""
    validate_release_request(
        request,
        deployment_repository=DEPLOY_REPOSITORY,
        deployment_ref=DEPLOY_REF,
    )
    image_tag = f"{CONTROL_IMAGE}:sha-{request.sha}"
    try:
        subprocess.run(
            ["docker", "pull", image_tag],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        inspected = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ReleaseVerificationError(
            "Docker CLI is unavailable on the deployment runner"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ReleaseVerificationError("Docker image verification timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ReleaseVerificationError(
            f"Docker could not inspect the published SHA image (exit {exc.returncode})"
        ) from exc

    try:
        records = json.loads(inspected.stdout)
        record = records[0]
        labels = record["Config"]["Labels"]
        repo_digests = tuple(record["RepoDigests"])
        if not isinstance(labels, dict):
            raise TypeError("Docker OCI labels are missing")
        return ReleaseImageEvidence(
            tag=image_tag,
            repository_digests=repo_digests,
            source=labels.get("org.opencontainers.image.source"),
            revision=labels.get("org.opencontainers.image.revision"),
            version=labels.get("org.opencontainers.image.version"),
        )
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("Docker image metadata is incomplete or invalid") from exc


def verify_environment(environment: Mapping[str, str] | None = None) -> VerifiedRelease:
    """Verify the current workflow event and return a fixed deployable digest."""
    env = environment if environment is not None else os.environ
    try:
        event_path = Path(env["GITHUB_EVENT_PATH"])
        deployment_repository = env["GITHUB_REPOSITORY"]
        deployment_ref = env["GITHUB_REF"]
        token = env["DECAGENT_PUBLIC_READ_TOKEN"]
    except KeyError as exc:
        raise ReleaseVerificationError(
            f"required workflow environment is missing: {exc.args[0]}"
        ) from exc
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("GitHub dispatch event file cannot be read") from exc
    if not isinstance(event, Mapping):
        raise ReleaseVerificationError("GitHub dispatch event must be an object")

    request = parse_dispatch_event(event)
    validate_release_request(
        request,
        deployment_repository=deployment_repository,
        deployment_ref=deployment_ref,
    )
    image = inspect_published_image(request)
    evidence = GitHubEvidenceClient(token).collect(request, image)
    return validate_release(
        request,
        evidence,
        deployment_repository=deployment_repository,
        deployment_ref=deployment_ref,
    )


def main() -> int:
    try:
        verified = verify_environment()
        output_path = os.environ.get("GITHUB_OUTPUT")
        if not output_path:
            raise ReleaseVerificationError("GITHUB_OUTPUT is required")
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"image={verified.image}\n")
        print(
            f"Verified {verified.tag} at {verified.sha}; "
            f"release run {verified.run_id} attempt {verified.run_attempt}."
        )
        return 0
    except ReleaseVerificationError as exc:
        print(f"Release verification rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
