import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import ops.deploy.release_verifier as verifier
from ops.deploy.release_verifier import (
    GitHubEvidenceClient,
    ReleaseEvidence,
    ReleaseImageEvidence,
    ReleaseJobEvidence,
    ReleaseRequest,
    ReleaseRunEvidence,
    ReleaseVerificationError,
    inspect_published_image,
    parse_dispatch_event,
    validate_release,
)

SOURCE_REPOSITORY = "shuaibilx/DecAgent"
DEPLOY_REPOSITORY = "shuaibilx/DecAgent-deploy"
TAG = "v0.2.0"
SHA = "a" * 40
MAIN_SHA = "b" * 40
IMAGE = "ghcr.io/shuaibilx/decagent-control@sha256:" + "c" * 64


def request():
    return ReleaseRequest(
        source_repository=SOURCE_REPOSITORY,
        tag=TAG,
        sha=SHA,
        run_id=7123456789,
        run_attempt=2,
        image=IMAGE,
    )


def evidence():
    return ReleaseEvidence(
        tag_commit_sha=SHA,
        main_sha=MAIN_SHA,
        comparison_status="ahead",
        ahead_by=3,
        behind_by=0,
        run=ReleaseRunEvidence(
            run_id=7123456789,
            run_attempt=2,
            workflow_path=".github/workflows/release.yml",
            head_branch=TAG,
            event="push",
            status="completed",
            conclusion="success",
            head_sha=SHA,
        ),
        jobs=(
            ReleaseJobEvidence("Full CI release gate", "completed", "success"),
            ReleaseJobEvidence("Publish versioned GHCR images", "completed", "success"),
        ),
        image=ReleaseImageEvidence(
            tag=f"ghcr.io/shuaibilx/decagent-control:sha-{SHA}",
            repository_digests=(IMAGE,),
            source="https://github.com/shuaibilx/DecAgent",
            revision=SHA,
            version=TAG,
        ),
    )


def verify(request_value=None, evidence_value=None, **kwargs):
    return validate_release(
        request_value or request(),
        evidence_value or evidence(),
        deployment_repository=DEPLOY_REPOSITORY,
        deployment_ref="refs/heads/main",
        **kwargs,
    )


def test_valid_release_evidence_returns_only_fixed_digest_and_run_identity():
    verified = verify()

    assert verified.image == IMAGE
    assert verified.tag == TAG
    assert verified.sha == SHA
    assert verified.run_id == 7123456789
    assert verified.run_attempt == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"source_repository": "attacker/DecAgent"},
        {"tag": "v0.2.0-rc.1"},
        {"tag": "v0.2.0; Invoke-Expression"},
        {"sha": "A" * 40},
        {"sha": "a" * 39},
        {"run_id": 0},
        {"run_attempt": 0},
        {"image": "ghcr.io/shuaibilx/decagent-control:latest"},
        {"image": "ghcr.io/attacker/decagent-control@sha256:" + "c" * 64},
    ],
)
def test_invalid_request_values_are_rejected(changes):
    with pytest.raises(ReleaseVerificationError):
        verify(replace(request(), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"tag_commit_sha": "d" * 40},
        {"comparison_status": "diverged"},
        {"comparison_status": "behind", "ahead_by": 0, "behind_by": 1},
        {"behind_by": 1},
        {"main_sha": "not-a-sha"},
    ],
)
def test_release_tag_must_resolve_to_sha_on_main_history(changes):
    with pytest.raises(ReleaseVerificationError):
        verify(evidence_value=replace(evidence(), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"comparison_status": "identical", "ahead_by": 1},
        {"comparison_status": "ahead", "ahead_by": 0},
    ],
)
def test_git_compare_status_must_match_commit_counts(changes):
    with pytest.raises(ReleaseVerificationError, match="status and counts disagree"):
        verify(evidence_value=replace(evidence(), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": 99},
        {"run_attempt": 1},
        {"workflow_path": ".github/workflows/untrusted.yml"},
        {"head_branch": "main"},
        {"event": "pull_request"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"head_sha": "d" * 40},
    ],
)
def test_release_run_must_be_the_successful_requested_attempt(changes):
    run = replace(evidence().run, **changes)
    with pytest.raises(ReleaseVerificationError):
        verify(evidence_value=replace(evidence(), run=run))


@pytest.mark.parametrize(
    "jobs",
    [
        (ReleaseJobEvidence("Full CI release gate", "completed", "failure"),),
        (ReleaseJobEvidence("Full CI release gate", "completed", "success"),),
        (
            ReleaseJobEvidence("Full CI release gate", "completed", "success"),
            ReleaseJobEvidence("Publish versioned GHCR images", "queued", None),
        ),
    ],
)
def test_quality_and_publish_jobs_must_both_succeed(jobs):
    with pytest.raises(ReleaseVerificationError):
        verify(evidence_value=replace(evidence(), jobs=jobs))


@pytest.mark.parametrize(
    "changes",
    [
        {"tag": "ghcr.io/shuaibilx/decagent-control:latest"},
        {"repository_digests": ()},
        {"source": "https://github.com/attacker/DecAgent"},
        {"revision": "d" * 40},
        {"version": "v0.1.9"},
    ],
)
def test_image_digest_must_match_fixed_sha_tag_and_release_labels(changes):
    image = replace(evidence().image, **changes)
    with pytest.raises(ReleaseVerificationError):
        verify(evidence_value=replace(evidence(), image=image))


@pytest.mark.parametrize(
    "extra_input",
    [
        {"script_path": "C:\\malicious\\deploy.ps1"},
        {"command": "Remove-Item -Recurse C:\\"},
        {"working_directory": ".."},
    ],
)
def test_dispatch_rejects_any_user_controlled_execution_input(extra_input):
    event = {
        "inputs": {
            "source_repository": SOURCE_REPOSITORY,
            "tag": TAG,
            "sha": SHA,
            "run_id": "7123456789",
            "run_attempt": "2",
            "image": IMAGE,
            **extra_input,
        }
    }

    with pytest.raises(ReleaseVerificationError, match="unexpected dispatch input"):
        parse_dispatch_event(event)


def test_dispatch_parses_only_the_minimal_fixed_input_contract():
    event = {
        "ref": "refs/heads/main",
        "inputs": {
            "source_repository": SOURCE_REPOSITORY,
            "tag": TAG,
            "sha": SHA,
            "run_id": "7123456789",
            "run_attempt": "2",
            "image": IMAGE,
        },
    }

    assert parse_dispatch_event(event) == request()


@pytest.mark.parametrize(
    "deployment_repository,deployment_ref",
    [
        (SOURCE_REPOSITORY, "refs/heads/main"),
        (DEPLOY_REPOSITORY, "refs/heads/feature/untrusted"),
    ],
)
def test_verifier_only_runs_from_the_private_deployment_main(deployment_repository, deployment_ref):
    with pytest.raises(ReleaseVerificationError):
        validate_release(
            request(),
            evidence(),
            deployment_repository=deployment_repository,
            deployment_ref=deployment_ref,
        )


def test_private_workflow_is_manual_fixed_ref_and_uses_only_the_private_runner():
    path = Path("ops/deploy/private-deploy-workflow.yml")
    workflow_text = path.read_text(encoding="utf-8")

    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "source_repository",
        "tag",
        "sha",
        "run_id",
        "run_attempt",
        "image",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"

    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "decagent-local-production"
    assert job["runs-on"] == ["self-hosted", "Windows", "X64", "decagent-deploy"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] == "false"
    assert all("repository" not in step.get("with", {}) for step in job["steps"])
    assert all("script_path" not in str(step) for step in job["steps"])
    assert all("command" not in step.get("with", {}) for step in job["steps"])
    assert "DECAGENT_PUBLIC_READ_TOKEN" not in workflow_text
    assert "packages: read" not in workflow_text
    assert "docker/login-action" not in workflow_text
    assert "${{ github.token }}" not in workflow_text
    assert "repository_dispatch" not in workflow_text
    verify_step = next(i for i, step in enumerate(job["steps"]) if step.get("id") == "verify")
    rollout_step = next(
        i
        for i, step in enumerate(job["steps"])
        if step.get("name") == "Roll out the verified digest through the fixed coordinator"
    )
    assert verify_step < rollout_step
    assert "${{ steps.verify.outputs.image }}" in workflow_text


def test_github_client_collects_release_and_attempt_evidence_from_fixed_repository():
    base = f"https://api.github.com/repos/{SOURCE_REPOSITORY}"
    annotated_tag_sha = "d" * 40
    responses = {
        f"{base}/git/ref/tags/{TAG}": {"object": {"type": "tag", "sha": annotated_tag_sha}},
        f"{base}/git/tags/{annotated_tag_sha}": {"object": {"type": "commit", "sha": SHA}},
        f"{base}/git/ref/heads/main": {"object": {"sha": MAIN_SHA}},
        f"{base}/compare/{SHA}...{MAIN_SHA}": {
            "status": "ahead",
            "ahead_by": 3,
            "behind_by": 0,
        },
        f"{base}/actions/runs/7123456789/attempts/2": {
            "id": 7123456789,
            "run_attempt": 2,
            "path": ".github/workflows/release.yml",
            "head_branch": TAG,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_sha": SHA,
        },
        f"{base}/actions/runs/7123456789/attempts/2/jobs?per_page=100": {
            "jobs": [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in ("Full CI release gate", "Publish versioned GHCR images")
            ]
        },
    }
    seen_urls = []

    def opener(request, timeout):
        seen_urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer test-token"
        assert timeout == 30
        return io.BytesIO(json.dumps(responses[request.full_url]).encode("utf-8"))

    client = GitHubEvidenceClient("test-token", opener=opener)
    gathered = client.collect(request(), evidence().image)

    assert gathered.tag_commit_sha == SHA
    assert gathered.main_sha == MAIN_SHA
    assert gathered.run == evidence().run
    assert {job.name for job in gathered.jobs} == {
        "Full CI release gate",
        "Publish versioned GHCR images",
    }
    assert seen_urls[0] == f"{base}/git/ref/tags/{TAG}"
    assert all(SOURCE_REPOSITORY in url for url in seen_urls)


def test_github_client_reads_public_provenance_without_a_personal_token():
    seen = []

    def opener(request_value, timeout):
        seen.append(request_value)
        assert timeout == 30
        return io.BytesIO(b'{"object":{"sha":"' + SHA.encode() + b'"}}')

    payload = GitHubEvidenceClient(opener=opener)._get("/git/ref/heads/main")

    assert payload["object"]["sha"] == SHA
    assert "Authorization" not in seen[0].headers


def test_github_api_errors_do_not_echo_token_or_response_body():
    def opener(request, timeout):
        raise verifier.urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b"secret-bearing upstream response"),
        )

    client = GitHubEvidenceClient("test-token", opener=opener)
    with pytest.raises(ReleaseVerificationError, match="HTTP 403") as error:
        client.tag_commit_sha(TAG)

    assert "test-token" not in str(error.value)
    assert "secret-bearing" not in str(error.value)


def test_docker_inspector_pulls_only_a_fixed_sha_tag_and_reads_oci_provenance(monkeypatch):
    calls = []
    docker_image = {
        "RepoDigests": [IMAGE],
        "Config": {
            "Labels": {
                "org.opencontainers.image.source": "https://github.com/shuaibilx/DecAgent",
                "org.opencontainers.image.revision": SHA,
                "org.opencontainers.image.version": TAG,
            }
        },
    }

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "pull"]:
            return SimpleNamespace(stdout="")
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([docker_image]))
        raise AssertionError(f"unexpected Docker command: {args}")

    monkeypatch.setattr(verifier.subprocess, "run", run)
    inspected = inspect_published_image(request())

    assert calls == [
        ["docker", "pull", f"{verifier.CONTROL_IMAGE}:sha-{SHA}"],
        ["docker", "image", "inspect", f"{verifier.CONTROL_IMAGE}:sha-{SHA}"],
    ]
    assert inspected.repository_digests == (IMAGE,)
    assert inspected.revision == SHA


def test_docker_inspector_rejects_invalid_request_before_running_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid request must be rejected before Docker access")

    monkeypatch.setattr(verifier.subprocess, "run", forbidden)
    with pytest.raises(ReleaseVerificationError):
        inspect_published_image(replace(request(), sha="x;whoami"))
