import pytest

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

    def ensure_ready(self):
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
