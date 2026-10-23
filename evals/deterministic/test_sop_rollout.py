import json

import pytest

from ops.deploy import sop_rollout as rollout_module
from ops.deploy.sop_rollout import (
    SchemaCompatibilityError,
    SopDockerRuntime,
    _safe_compose_config,
    exclusive_rollout_lock,
    rollout,
)

PREVIOUS = "ghcr.io/shuaibilx/decagent-control@sha256:" + "a" * 64
CANDIDATE = "ghcr.io/shuaibilx/decagent-control@sha256:" + "b" * 64


class FakeRuntime:
    def __init__(
        self,
        *,
        readiness=(True,),
        incompatible_images=(),
        interrupted_images=(),
        backup_error=None,
    ):
        self.current_image = PREVIOUS
        self.readiness = list(readiness)
        self.incompatible_images = set(incompatible_images)
        self.interrupted_images = set(interrupted_images)
        self.backup_error = backup_error
        self.applied = []
        self.stopped = 0
        self.backups = 0
        self.services_running = True
        self.events = []
        self.pristine = True
        self.recovery_valid = True
        self.compose_sha256 = "c" * 64

    def ensure_ready(self):
        if getattr(self, "stack_ready", True) is False:
            raise RuntimeError("Existing SOP stack is not business-ready")
        return None

    def capture_release(self):
        return {
            "image": self.current_image,
            "compose_sha256": "c" * 64,
            "schema_versions": [1, 2, 3],
            "graph_versions": ["business-2", "business-3"],
            "main_api_key": "sk-secret-test-value",
            "database_password": "secret-password",
        }

    def create_backup(self, run_id, _previous):
        self.backups += 1
        self.events.append("backup")
        assert not self.services_running, "application writers must be stopped during backup"
        if self.backup_error:
            raise self.backup_error
        return {"path": f"backup-{run_id}", "sha256": "d" * 64}

    def apply_image(self, image):
        self.events.append(f"apply:{image}")
        self.applied.append(image)
        self.current_image = image
        self.services_running = True
        if image in self.interrupted_images:
            raise KeyboardInterrupt
        if image in self.incompatible_images:
            raise SchemaCompatibilityError("incompatible database version")

    def wait_ready(self):
        return self.readiness.pop(0) if self.readiness else False

    def stop_application(self):
        self.stopped += 1
        self.events.append("stop")
        self.services_running = False

    def assert_initial_install_target_pristine(self, _image):
        self.events.append("assert-pristine")
        if not self.pristine:
            raise RuntimeError("SOP initial installation target contains Docker state")

    def assert_initial_install_recovery_target(self, _image, compose_sha256):
        self.events.append("assert-recovery")
        if not self.recovery_valid or compose_sha256 != self.compose_sha256:
            raise RuntimeError("SOP initial installation recovery target changed")

    def compose_fingerprint(self, _image):
        return self.compose_sha256

    def install_initial_image(self, image):
        self.events.append(f"initial-install:{image}")
        self.applied.append(image)
        self.current_image = image
        self.pristine = False
        self.services_running = True


def test_successful_rollout_records_versions_backup_and_image_without_secrets(tmp_path):
    runtime = FakeRuntime(readiness=(True,))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-1")

    assert result["status"] == "deployed"
    assert result["previous"]["image"] == PREVIOUS
    assert result["candidate"]["image"] == CANDIDATE
    assert result["previous"]["schema_versions"] == [1, 2, 3]
    assert result["previous"]["graph_versions"] == ["business-2", "business-3"]
    assert result["backup"] == {"path": "backup-run-1", "sha256": "d" * 64}
    assert runtime.applied == [CANDIDATE]
    assert runtime.events.index("stop") < runtime.events.index("backup")
    assert "secret" not in (tmp_path / "release.json").read_text(encoding="utf-8").lower()


def test_failed_health_check_rolls_back_only_compatible_application_image(tmp_path):
    runtime = FakeRuntime(readiness=(False, True))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-2")

    assert result["status"] == "rolled_back"
    assert runtime.applied == [CANDIDATE, PREVIOUS]
    assert runtime.stopped == 2
    assert result["database_restored"] is False


def test_operator_interrupt_during_candidate_apply_rolls_back_previous_image(tmp_path):
    runtime = FakeRuntime(interrupted_images=(CANDIDATE,), readiness=(True,))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-cancel")

    assert result["status"] == "rolled_back"
    assert result["candidate_failure"] == "KeyboardInterrupt"
    assert runtime.applied == [CANDIDATE, PREVIOUS]
    assert runtime.services_running


def test_incompatible_schema_stops_services_and_preserves_database(tmp_path):
    runtime = FakeRuntime(readiness=(False,), incompatible_images=(PREVIOUS,))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-3")

    assert result["status"] == "rollback_blocked"
    assert runtime.applied == [CANDIDATE, PREVIOUS]
    assert runtime.stopped == 3
    assert result["database_restored"] is False
    assert result["database_preserved"] is True


def test_docker_runtime_stops_mcp_with_api_and_worker(monkeypatch):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    calls = []
    monkeypatch.setattr(runtime, "_compose", lambda arguments, **kwargs: calls.append(arguments))

    runtime.stop_application()

    assert calls == [["stop", "api", "worker", "business-mcp"]]


def test_compose_fingerprint_is_bound_to_the_active_immutable_image(tmp_path):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    runtime.root = tmp_path
    rendered_images = []

    def render(image=None):
        rendered_images.append(image)
        return {
            "services": {
                name: {"image": image}
                for name in ("migrate", "bootstrap", "business-mcp", "api", "worker")
            }
        }

    runtime._render_config = render

    fingerprint = runtime._compose_fingerprint(CANDIDATE)

    assert rendered_images == [CANDIDATE]
    assert len(fingerprint) == 64


def test_compose_fingerprint_changes_when_a_secret_file_changes(tmp_path):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    runtime.root = tmp_path
    secret_file = tmp_path / "main_api_key"
    secret_file.write_text("first-secret-value", encoding="utf-8")
    config = {
        "services": {
            name: {"image": CANDIDATE, "secrets": ["main_api_key"]}
            for name in ("migrate", "bootstrap", "business-mcp", "api", "worker")
        },
        "secrets": {"main_api_key": {"file": str(secret_file)}},
    }
    runtime._render_config = lambda _image: config

    initial_fingerprint = runtime._compose_fingerprint(CANDIDATE)
    secret_file.write_text("rotated-secret-value", encoding="utf-8")
    rotated_fingerprint = runtime._compose_fingerprint(CANDIDATE)

    assert initial_fingerprint != rotated_fingerprint
    assert "first-secret-value" not in initial_fingerprint
    assert "rotated-secret-value" not in rotated_fingerprint


@pytest.mark.parametrize("resource_kind", ["container", "volume", "network"])
def test_initial_install_refuses_each_kind_of_existing_compose_resource(
    monkeypatch, resource_kind
):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    config = {
        "name": "decagent-sop",
        "services": {},
        "volumes": {"sop_data": {}, "sop_postgres_data": {}},
        "networks": {"sop-internal": {}, "sop-egress": {}},
    }
    monkeypatch.setattr(runtime, "_render_config", lambda _image: config)

    def docker(arguments, **_kwargs):
        command = arguments[0]
        if command == "ps":
            return "container-id" if resource_kind == "container" else ""
        if command == "volume":
            return "decagent-sop_sop_data" if resource_kind == "volume" else ""
        if command == "network":
            return "decagent-sop_sop-internal" if resource_kind == "network" else ""
        raise AssertionError(f"unexpected docker command: {arguments}")

    monkeypatch.setattr(runtime, "_docker", docker)

    with pytest.raises(RuntimeError, match="contains Docker state"):
        runtime.assert_initial_install_target_pristine(CANDIDATE)


def test_initial_install_recovery_rejects_container_from_another_release(monkeypatch):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    config = {
        "name": "decagent-sop",
        "services": {
            "api": {"image": CANDIDATE},
            "worker": {"image": CANDIDATE},
            "business-mcp": {"image": CANDIDATE},
        },
        "volumes": {},
        "networks": {},
    }
    monkeypatch.setattr(runtime, "_render_config", lambda _image: config)
    monkeypatch.setattr(runtime, "_compose_fingerprint", lambda _image: "c" * 64)

    def docker(arguments, **_kwargs):
        if arguments[0] == "ps":
            return "container-id"
        if arguments[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Config": {
                            "Image": PREVIOUS,
                            "Labels": {
                                "com.docker.compose.project": "decagent-sop",
                                "com.docker.compose.service": "api",
                            },
                        }
                    }
                ]
            )
        raise AssertionError(f"unexpected docker command: {arguments}")

    monkeypatch.setattr(runtime, "_docker", docker)

    with pytest.raises(RuntimeError, match="unexpected containers"):
        runtime.assert_initial_install_recovery_target(CANDIDATE, "c" * 64)


@pytest.mark.parametrize("resource_kind", ["volume", "network"])
def test_initial_install_recovery_rejects_unexpected_project_resource(
    monkeypatch, resource_kind
):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    config = {
        "name": "decagent-sop",
        "services": {},
        "volumes": {"sop_postgres_data": {}},
        "networks": {"sop-internal": {}},
    }
    monkeypatch.setattr(runtime, "_render_config", lambda _image: config)
    monkeypatch.setattr(runtime, "_compose_fingerprint", lambda _image: "c" * 64)

    def docker(arguments, **_kwargs):
        if arguments[0] == "ps":
            return ""
        if arguments[0] == "volume" and arguments[1] == "ls":
            if resource_kind == "volume" and "--filter" in arguments:
                return "decagent-sop_orphaned"
            return ""
        if arguments[0] == "network" and arguments[1] == "ls":
            if resource_kind == "network" and "--filter" in arguments:
                return "decagent-sop_orphaned"
            return ""
        raise AssertionError(f"unexpected docker command: {arguments}")

    monkeypatch.setattr(runtime, "_docker", docker)

    with pytest.raises(RuntimeError, match="unexpected (volumes|networks)"):
        runtime.assert_initial_install_recovery_target(CANDIDATE, "c" * 64)


def test_initial_install_pulls_all_images_before_starting_without_build(monkeypatch):
    runtime = SopDockerRuntime.__new__(SopDockerRuntime)
    monkeypatch.setattr(runtime, "_render_config", lambda _image: {})
    calls = []
    monkeypatch.setattr(runtime, "_compose", lambda arguments, **kwargs: calls.append(arguments))

    runtime.install_initial_image(CANDIDATE)

    assert calls == [
        ["pull"],
        ["up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180"],
    ]


def test_backup_failure_restores_previous_service_before_returning(tmp_path):
    runtime = FakeRuntime(backup_error=OSError("disk full"))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-4")

    assert result["status"] == "backup_failed_recovered"
    assert result["preparation_failure"] == "OSError"
    assert result["active_image"] == PREVIOUS
    assert runtime.backups == 1
    assert runtime.applied == [PREVIOUS]
    assert runtime.services_running
    assert result["backup"] is None
    assert (tmp_path / "release.json").exists()


def test_backup_failure_records_when_previous_service_cannot_be_recovered(tmp_path):
    runtime = FakeRuntime(backup_error=OSError("disk full"), incompatible_images=(PREVIOUS,))

    result = rollout(runtime, CANDIDATE, tmp_path / "release.json", run_id="run-4b")

    assert result["status"] == "backup_failed_recovery_failed"
    assert result["active_image"] is None
    assert result["recovery_failure"] == "SchemaCompatibilityError"
    assert not runtime.services_running
    assert (tmp_path / "release.json").exists()


def test_existing_release_record_is_never_overwritten(tmp_path):
    runtime = FakeRuntime()
    record = tmp_path / "release.json"
    record.write_text("historical", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        rollout(runtime, CANDIDATE, record, run_id="run-6")

    assert record.read_text(encoding="utf-8") == "historical"
    assert runtime.backups == 0 and runtime.applied == []


def test_release_lock_rejects_concurrent_deployment_and_clears_only_its_own_lock(tmp_path):
    lock = tmp_path / "sop-rollout.lock"

    with exclusive_rollout_lock(lock, "run-7"):
        with pytest.raises(RuntimeError, match="Another SOP deployment"):
            with exclusive_rollout_lock(lock, "run-8"):
                pass
        assert "run-7" in lock.read_text(encoding="utf-8")

    assert not lock.exists()


def test_compose_record_redacts_secret_values_but_keeps_operational_config():
    safe = _safe_compose_config(
        {
            "services": {
                "api": {
                    "environment": {
                        "DECAGENT_MAIN_API_KEY": "sk-secret",
                        "DECAGENT_DATABASE_URL": "postgres://user:password@db/app",
                        "DECAGENT_MAIN_MODEL": "qwen3.8-flash",
                    }
                }
            }
        }
    )

    environment = safe["services"]["api"]["environment"]
    assert environment["DECAGENT_MAIN_API_KEY"] == "[redacted]"
    assert environment["DECAGENT_DATABASE_URL"] == "[redacted]"
    assert environment["DECAGENT_MAIN_MODEL"] == "qwen3.8-flash"


@pytest.mark.parametrize(
    "image", ["decagent:latest", "ghcr.io/acme/app:latest", "ghcr.io/a/b@sha256:abc"]
)
def test_rollout_rejects_mutable_or_untrusted_image_refs(tmp_path, image):
    runtime = FakeRuntime()

    with pytest.raises(ValueError, match="immutable GHCR control-image digest"):
        rollout(runtime, image, tmp_path / "release.json", run_id="run-5")

    assert runtime.backups == 0
    assert runtime.applied == []


def test_initial_install_coordinator_exists_before_it_is_used():
    assert callable(getattr(rollout_module, "first_install", None)), (
        "SOP deployment needs an explicit first-install coordinator"
    )


def test_deployment_entrypoint_selects_first_install_only_when_no_ready_stack_exists(tmp_path):
    runtime = FakeRuntime(readiness=(True,))
    runtime.stack_ready = False
    initial_record = tmp_path / "state" / "initial-install.json"

    result = rollout_module.deploy_candidate(
        runtime,
        CANDIDATE,
        tmp_path / "state" / "release.json",
        initial_record,
        run_id="initial-dispatch-1",
    )

    assert result["status"] == "ready"
    assert initial_record.exists()
    assert runtime.backups == 0
    assert runtime.applied == [CANDIDATE]


def test_deployment_entrypoint_is_idempotent_for_already_active_digest(tmp_path):
    runtime = FakeRuntime(readiness=(True,))
    runtime.current_image = CANDIDATE
    initial_record = tmp_path / "state" / "initial-install.json"
    initial_record.parent.mkdir()
    initial_record.write_text(
        json.dumps(
            {
                "format": 1,
                "status": "ready",
                "candidate": {"image": CANDIDATE},
                "compose_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )

    result = rollout_module.deploy_candidate(
        runtime,
        CANDIDATE,
        tmp_path / "state" / "release.json",
        initial_record,
        run_id="duplicate-release-1",
    )

    assert result["status"] == "ready"
    assert result["already_active"] is True
    assert runtime.applied == []
    assert runtime.backups == 0


def test_initial_install_bootstraps_only_a_pristine_target_and_persists_digest(tmp_path):
    runtime = FakeRuntime(readiness=(True,))
    record_path = tmp_path / "state" / "initial-install.json"

    result = rollout_module.first_install(
        runtime, CANDIDATE, record_path, run_id="first-install-1"
    )

    assert result["status"] == "ready"
    assert result["candidate"]["image"] == CANDIDATE
    assert result["compose_sha256"] == "c" * 64
    assert runtime.events[:3] == [
        "assert-pristine",
        "assert-pristine",
        f"initial-install:{CANDIDATE}",
    ]
    assert json.loads(record_path.read_text(encoding="utf-8"))["status"] == "ready"


def test_initial_install_refuses_existing_docker_state_without_touching_it(tmp_path):
    runtime = FakeRuntime()
    runtime.pristine = False
    record_path = tmp_path / "state" / "initial-install.json"

    with pytest.raises(RuntimeError, match="contains Docker state"):
        rollout_module.first_install(runtime, CANDIDATE, record_path, run_id="first-install-2")

    assert not record_path.exists()
    assert runtime.applied == []
    assert runtime.backups == 0


def test_failed_initial_install_preserves_state_and_only_retries_same_image(tmp_path):
    runtime = FakeRuntime(readiness=(False, True))
    record_path = tmp_path / "state" / "initial-install.json"

    failed = rollout_module.first_install(
        runtime, CANDIDATE, record_path, run_id="first-install-3"
    )
    assert failed["status"] == "failed"
    assert failed["database_preserved"] is True
    assert runtime.stopped == 1

    with pytest.raises(ValueError, match="same immutable image"):
        rollout_module.first_install(
            runtime, PREVIOUS, record_path, run_id="first-install-4"
        )
    assert runtime.applied == [CANDIDATE]

    recovered = rollout_module.first_install(
        runtime, CANDIDATE, record_path, run_id="first-install-5"
    )
    assert recovered["status"] == "ready"
    assert runtime.applied == [CANDIDATE, CANDIDATE]


def test_initial_install_rejects_changed_compose_fingerprint_on_recovery(tmp_path):
    runtime = FakeRuntime(readiness=(False,))
    record_path = tmp_path / "state" / "initial-install.json"
    rollout_module.first_install(runtime, CANDIDATE, record_path, run_id="first-install-6")
    runtime.compose_sha256 = "e" * 64

    with pytest.raises(RuntimeError, match="recovery target changed"):
        rollout_module.first_install(
            runtime, CANDIDATE, record_path, run_id="first-install-7"
        )

    assert runtime.applied == [CANDIDATE]
