"""Fail-closed SOP application rollout with immutable-image rollback records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

IMAGE_PATTERN = re.compile(r"^ghcr\.io/shuaibilx/decagent-control@sha256:[a-f0-9]{64}$")
RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")


class SchemaCompatibilityError(RuntimeError):
    """The selected application image cannot run against the current database."""


def _validate_image(image: str) -> None:
    if not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
        raise ValueError("SOP rollout requires an immutable GHCR control-image digest")


def _release_snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("SOP release metadata must be a mapping")
    image = value.get("image")
    _validate_image(image)
    schema_versions = value.get("schema_versions")
    graph_versions = value.get("graph_versions")
    compose_sha256 = value.get("compose_sha256")
    if (
        not isinstance(schema_versions, list)
        or any(type(version) is not int or version < 1 for version in schema_versions)
        or schema_versions != sorted(set(schema_versions))
    ):
        raise ValueError("SOP schema version metadata is invalid")
    if (
        not isinstance(graph_versions, list)
        or any(not isinstance(version, str) or not version for version in graph_versions)
        or graph_versions != sorted(set(graph_versions))
    ):
        raise ValueError("SOP graph version metadata is invalid")
    if not isinstance(compose_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", compose_sha256) is None:
        raise ValueError("SOP Compose configuration fingerprint is invalid")
    # Whitelist persisted fields. Runtime metadata may contain credential paths or values.
    return {
        "image": image,
        "compose_sha256": compose_sha256,
        "schema_versions": schema_versions,
        "graph_versions": graph_versions,
    }


def _write_record(path: Path, record: dict, *, create: bool = False) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("SOP release record cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if create and path.exists():
        raise FileExistsError("SOP release record already exists; prior state is preserved")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create and path.exists():
            raise FileExistsError("SOP release record already exists; prior state is preserved")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def rollout(runtime, candidate_image: str, record_path: Path, *, run_id: str) -> dict:
    """Deploy an immutable image or restore the previous compatible app image.

    Database backups are never restored over the live database. If the old app cannot
    start against the migrated schema, the app services stop and the database remains
    untouched for a deliberate forward recovery.
    """
    _validate_image(candidate_image)
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("SOP rollout run id is invalid")
    path = Path(record_path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("SOP release record already exists; prior state is preserved")

    runtime.ensure_ready()
    previous = _release_snapshot(runtime.capture_release())
    if previous["image"] == candidate_image:
        raise ValueError("Candidate image is already active")
    record = {
        "format": 1,
        "run_id": run_id,
        "status": "preparing_backup",
        "previous": previous,
        "candidate": {"image": candidate_image},
        "backup": None,
        "database_restored": False,
        "database_preserved": True,
    }
    _write_record(path, record, create=True)

    try:
        # Quiesce every business writer so the database dump and /data snapshot agree.
        runtime.stop_application()
        backup = runtime.create_backup(run_id, previous)
        if (
            not isinstance(backup, dict)
            or not isinstance(backup.get("path"), str)
            or not isinstance(backup.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", backup["sha256"]) is None
        ):
            raise ValueError("SOP backup did not return a verified manifest")
    except (Exception, KeyboardInterrupt) as preparation_error:
        record["preparation_failure"] = type(preparation_error).__name__
        try:
            runtime.apply_image(previous["image"])
            if not runtime.wait_ready():
                raise RuntimeError("previous_readiness_failed_after_backup_error")
            record["status"] = "backup_failed_recovered"
            record["active_image"] = previous["image"]
        except (Exception, KeyboardInterrupt) as recovery_error:
            stop_failure = _stop_application_without_raising(runtime)
            record["status"] = "backup_failed_recovery_failed"
            record["active_image"] = None
            record["recovery_failure"] = type(recovery_error).__name__
            if stop_failure is not None:
                record["recovery_stop_failure"] = stop_failure
        _write_record(path, record)
        return record

    record["backup"] = {"path": backup["path"], "sha256": backup["sha256"]}
    record["status"] = "prepared"
    _write_record(path, record)

    try:
        record["status"] = "applying"
        _write_record(path, record)
        runtime.apply_image(candidate_image)
        record["candidate"]["observed"] = _release_snapshot(runtime.capture_release())
        if not runtime.wait_ready():
            raise RuntimeError("candidate_readiness_failed")
        record["status"] = "deployed"
        record["active_image"] = candidate_image
        _write_record(path, record)
        return record
    except (Exception, KeyboardInterrupt) as candidate_error:
        record["status"] = "rolling_back"
        record["candidate_failure"] = type(candidate_error).__name__
        _write_record(path, record)
        try:
            runtime.stop_application()
            runtime.apply_image(previous["image"])
            if not runtime.wait_ready():
                raise RuntimeError("previous_readiness_failed")
            record["status"] = "rolled_back"
            record["active_image"] = previous["image"]
        except SchemaCompatibilityError:
            stop_failure = _stop_application_without_raising(runtime)
            record["status"] = "rollback_blocked" if stop_failure is None else "rollback_failed"
            record["active_image"] = None
            record["rollback_failure"] = "schema_incompatible"
            if stop_failure is not None:
                record["rollback_stop_failure"] = stop_failure
        except (Exception, KeyboardInterrupt) as rollback_error:
            stop_failure = _stop_application_without_raising(runtime)
            record["status"] = "rollback_failed"
            record["active_image"] = None
            record["rollback_failure"] = type(rollback_error).__name__
            if stop_failure is not None:
                record["rollback_stop_failure"] = stop_failure
        _write_record(path, record)
        return record


def _stop_application_without_raising(runtime) -> str | None:
    try:
        runtime.stop_application()
    except (Exception, KeyboardInterrupt) as exc:
        return type(exc).__name__
    return None


_SECRET_FIELD = re.compile(
    r"secret|password|token|api.?key|credential|private.?key|database.?url|dsn|connection.?string",
    re.I,
)
_APP_SERVICES = ("migrate", "bootstrap", "business-mcp", "api", "worker")
_COMPATIBILITY_ERRORS = (
    "database schema is newer than this application",
    "incompatible database schema",
    "active task has incompatible graph version",
    "migration checksum differs",
    "migration history contains a gap",
)


def _safe_compose_config(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _SECRET_FIELD.search(str(key)) else _safe_compose_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_compose_config(item) for item in value]
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> list[dict]:
    root = Path(root)
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("SOP backup data contains a symlink")
        if path.is_file():
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    return result


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami"], capture_output=True, text=True, encoding="utf-8", check=False, timeout=10
        )
        if identity.returncode != 0 or not identity.stdout.strip():
            raise RuntimeError("Could not determine the Windows backup owner")
        secured = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity.stdout.strip()}:(OI)(CI)F",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )
        if secured.returncode != 0:
            raise RuntimeError("Could not restrict SOP backup access to the current Windows user")
    else:
        path.chmod(0o700)


class SopDockerRuntime:
    """Docker Compose operations for one already-running, isolated SOP stack."""

    def __init__(self, project_root, compose_file, env_file, backup_root):
        self.root = Path(project_root).resolve()
        self.compose_file = Path(compose_file).resolve()
        self.env_file = Path(env_file).resolve()
        self.backup_root = Path(backup_root).resolve()
        if not self.compose_file.is_file() or not self.env_file.is_file():
            raise ValueError("SOP Compose file or environment file is missing")

    def _compose(self, arguments, *, image=None, stdout=None, timeout=180):
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "--env-file",
            str(self.env_file),
            "--file",
            str(self.compose_file),
            *arguments,
        ]
        environment = os.environ.copy()
        if image:
            environment["DECAGENT_SOP_IMAGE"] = image
        result = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE if stdout is None else stdout,
            stderr=subprocess.PIPE,
            text=False,
            check=False,
            timeout=timeout,
        )
        output = result.stdout.decode("utf-8", errors="replace") if stdout is None else ""
        error = result.stderr.decode("utf-8", errors="replace")
        if result.returncode:
            lowered = (output + "\n" + error).casefold()
            if any(message in lowered for message in _COMPATIBILITY_ERRORS):
                raise SchemaCompatibilityError(
                    "Application image is incompatible with database state"
                )
            raise RuntimeError(
                f"Docker Compose {arguments[0]} failed with exit {result.returncode}"
            )
        return output

    def _docker(self, arguments, *, timeout=180):
        result = subprocess.run(
            ["docker", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"Docker {arguments[0]} failed with exit {result.returncode}")
        return result.stdout.strip()

    def _render_config(self, image=None):
        rendered = self._compose(["config", "--format", "json"], image=image)
        try:
            value = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Docker Compose returned invalid configuration") from exc
        from ops.deploy.sop_preflight import validate_compose_config

        validate_compose_config(value)
        images = {value["services"][name].get("image") for name in _APP_SERVICES}
        if len(images) != 1 or None in images:
            raise ValueError("All SOP application services must use one immutable control image")
        if image and images != {image}:
            raise ValueError("Rendered SOP image differs from the requested image digest")
        return value

    def _compose_fingerprint(self):
        config = self._render_config()
        for name in _APP_SERVICES:
            config["services"][name]["image"] = "<immutable-control-image>"
        safe = _safe_compose_config(config)
        canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def ensure_ready(self):
        from ops.deploy.sop_business_probe import probe_running_sop

        result = probe_running_sop(
            self.root, self.compose_file, self.env_file, env=os.environ.copy()
        )
        if result.get("status") != "ready":
            raise RuntimeError(
                "Existing SOP stack is not business-ready; deployment was not started"
            )

    def capture_release(self):
        image_refs = []
        for service in ("business-mcp", "api", "worker"):
            container = self._compose(["ps", "--all", "--quiet", service]).strip()
            if not container:
                raise RuntimeError("A running SOP application container is missing")
            image_refs.append(self._docker(["inspect", "--format", "{{.Config.Image}}", container]))
        if len(set(image_refs)) != 1:
            raise ValueError("SOP application containers do not share one release image")
        image = image_refs[0]
        _validate_image(image)
        schema_text = self._compose(
            [
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "decagent",
                "-d",
                "decagent",
                "-X",
                "-At",
                "-c",
                "SELECT COALESCE(string_agg(version::text, ',' ORDER BY version), '') "
                "FROM app.schema_migrations",
            ]
        ).strip()
        schema_versions = [int(item) for item in schema_text.split(",") if item]
        graph_text = self._compose(
            [
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "decagent",
                "-d",
                "decagent",
                "-X",
                "-At",
                "-c",
                "SELECT COALESCE(string_agg(DISTINCT graph_version, ',' "
                "ORDER BY graph_version), '') "
                "FROM app.tasks",
            ]
        ).strip()
        graph_versions = sorted(item for item in graph_text.split(",") if item)
        return {
            "image": image,
            "compose_sha256": self._compose_fingerprint(),
            "schema_versions": schema_versions,
            "graph_versions": graph_versions,
        }

    def create_backup(self, run_id, previous):
        _validate_image(previous["image"])
        if self.backup_root.is_symlink():
            raise ValueError("SOP backup root cannot be a symlink")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        destination = self.backup_root / run_id
        _secure_directory(destination)
        dump = destination / "database.dump"
        with dump.open("xb") as stream:
            self._compose(
                ["exec", "-T", "postgres", "pg_dump", "-U", "decagent", "-d", "decagent", "-Fc"],
                stdout=stream,
                timeout=600,
            )
        if dump.stat().st_size == 0:
            raise RuntimeError("PostgreSQL backup is empty")
        if os.name != "nt":
            dump.chmod(0o600)
        api_container = self._compose(["ps", "--all", "--quiet", "api"]).strip()
        if not api_container:
            raise RuntimeError("SOP API container is missing for data-volume backup")
        self._docker(["cp", f"{api_container}:/data", str(destination / "data")], timeout=600)
        data_root = destination / "data"
        data = _tree_manifest(data_root) if data_root.exists() else []
        compose_config = _safe_compose_config(self._render_config(previous["image"]))
        compose_payload = json.dumps(
            compose_config, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        compose_snapshot = destination / "compose.json"
        with compose_snapshot.open("xb") as stream:
            stream.write(compose_payload + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            compose_snapshot.chmod(0o600)
        manifest = {
            "format": 1,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "previous": previous,
            "database_dump": {
                "path": "database.dump",
                "size": dump.stat().st_size,
                "sha256": _sha256_file(dump),
            },
            "compose": {
                "path": "compose.json",
                "sha256": _sha256_file(compose_snapshot),
                "secrets_redacted": True,
            },
            "data": data,
        }
        manifest_path = destination / "manifest.json"
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        with manifest_path.open("xb") as stream:
            stream.write(payload + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            manifest_path.chmod(0o600)
        return {"path": str(destination), "sha256": _sha256_file(manifest_path)}

    def apply_image(self, image):
        _validate_image(image)
        self._render_config(image)
        self._compose(["pull", *_APP_SERVICES], image=image, timeout=600)
        self._compose(
            ["up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180"],
            image=image,
            timeout=240,
        )

    def wait_ready(self):
        from ops.deploy.sop_business_probe import probe_running_sop

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                result = probe_running_sop(
                    self.root, self.compose_file, self.env_file, env=os.environ.copy()
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                result = {"status": "not_ready"}
            if result.get("status") == "ready":
                return True
            time.sleep(3)
        return False

    def stop_application(self):
        self._compose(["stop", "api", "worker", "business-mcp"], timeout=60)


@contextmanager
def exclusive_rollout_lock(path: Path, run_id: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("SOP rollout lock cannot be a symlink")
    payload = f"{os.getpid()} {run_id}\n".encode()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            "Another SOP deployment is active or a prior lock needs inspection"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            if path.read_bytes() == payload:
                path.unlink()
        except FileNotFoundError:
            pass


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--compose-file", default="compose.sop.yaml")
    parser.add_argument("--env-file", default="ops/deploy/sop.env")
    parser.add_argument("--backup-root", default=".decagent/sop-backups")
    parser.add_argument("--state-root", default=".decagent/sop-releases")
    parser.add_argument("--run-id", default=uuid4().hex)
    args = parser.parse_args(argv)
    root = Path(args.project_directory).resolve()
    compose_file = (root / args.compose_file).resolve()
    env_file = (root / args.env_file).resolve()
    backup_root = (root / args.backup_root).resolve()
    state_root = (root / args.state_root).resolve()
    record_path = state_root / f"{args.run_id}.json"
    lock_path = root / ".decagent" / "sop-rollout.lock"
    try:
        with exclusive_rollout_lock(lock_path, args.run_id):
            runtime = SopDockerRuntime(root, compose_file, env_file, backup_root)
            result = rollout(runtime, args.candidate_image, record_path, run_id=args.run_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "deployed" else 2
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"SOP rollout failed safely: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
