"""Broker-only Docker API client. No caller-supplied host paths or Docker configuration."""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
from uuid import uuid4

import httpx

from .archive import make_archive, read_archive
from .protocol import ExecutionRequest


class DockerEngine:
    def __init__(
        self,
        *,
        runner_image,
        proxy_container,
        socket_path="/var/run/docker.sock",
        control_containers=(),
    ):
        if not re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}", runner_image):
            raise ValueError("Runner must be pinned by immutable image digest")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", proxy_container):
            raise ValueError("Invalid proxy container")
        self.runner_image = runner_image
        self.proxy_container = proxy_container
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in control_containers):
            raise ValueError("Invalid control container")
        self.control_containers = tuple(control_containers)
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url="http://docker/v1.47",
            timeout=30,
            trust_env=False,
        )

    def api(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return (
            response.json()
            if response.content and "json" in response.headers.get("content-type", "")
            else response.content
        )

    @staticmethod
    def names(task_id):
        return {
            "network": f"decagent-task-{task_id}",
            "workspace": f"decagent-work-{task_id}",
            "baseline": f"decagent-base-{task_id}",
        }

    @staticmethod
    def container_name(execution_id):
        return f"decagent-exec-{execution_id}"

    def command_config(self, request: ExecutionRequest):
        names = self.names(request.task_id)
        command = {
            "argv": request.argv,
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
        }
        return self.config(
            request.task_id,
            ["python", "/opt/runner/runner.py", json.dumps(command)],
            network=names["network"],
            labels={
                "decagent.execution": request.execution_id,
                "decagent.generation": str(request.lease_generation),
            },
        )

    def config(self, task_id, argv, *, network="none", labels=None, baseline=False):
        names = self.names(task_id)
        return {
            "Image": self.runner_image,
            "User": "10001:10001",
            "Cmd": argv,
            "WorkingDir": "/workspace",
            "Env": [
                "HOME=/tmp/home",
                "TMPDIR=/tmp",
                "PYTHONUNBUFFERED=1",
                "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONPATH=/opt/runner",
                "HTTP_PROXY=http://egress-proxy:3128",
                "HTTPS_PROXY=http://egress-proxy:3128",
                "http_proxy=http://egress-proxy:3128",
                "https_proxy=http://egress-proxy:3128",
                "NO_PROXY=",
                "no_proxy=",
                "UV_NO_MANAGED_PYTHON=1",
                "UV_CACHE_DIR=/workspace/.cache/uv",
                "PIP_DISABLE_PIP_VERSION_CHECK=1",
            ],
            "Labels": {
                "decagent.managed": "executor",
                "decagent.task": task_id,
                "decagent.executor.instance": self.proxy_container,
                **(labels or {}),
            },
            "HostConfig": {
                "NetworkMode": network,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "NanoCpus": 2_000_000_000,
                "Memory": 4 * 1024**3,
                "MemorySwap": 4 * 1024**3,
                "PidsLimit": 128,
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=268435456,mode=1777"},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": names["baseline"] if baseline else names["workspace"],
                        "Target": "/workspace",
                    },
                    {
                        "Type": "volume",
                        "Source": names["baseline"],
                        "Target": "/baseline",
                        "ReadOnly": True,
                    },
                ],
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "2m", "max-file": "1"}},
                "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
            },
        }

    def create_container(self, name, config):
        return self.api("POST", "/containers/create", params={"name": name}, json=config)["Id"]

    def start(self, name):
        self.api("POST", f"/containers/{name}/start")

    def inspect(self, name):
        return self.api("GET", f"/containers/{name}/json")

    def kill(self, name):
        try:
            self.api("POST", f"/containers/{name}/kill", params={"signal": "SIGKILL"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {404, 409}:
                raise

    def remove_container(self, name):
        try:
            self.api("DELETE", f"/containers/{name}", params={"force": "true"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise

    def logs(self, name):
        data = self.api("GET", f"/containers/{name}/logs", params={"stdout": 1, "stderr": 1})
        return self.demultiplex(data, maximum=2_000_000).decode("utf-8", "replace")

    @staticmethod
    def demultiplex(data, *, maximum):
        # Docker's non-TTY streams have 8-byte multiplex headers.
        output = bytearray()
        while data:
            if len(data) < 8:
                raise ValueError("Invalid Docker output framing")
            length = int.from_bytes(data[4:8], "big")
            if length > len(data) - 8 or len(output) + length > maximum:
                raise ValueError("Docker output exceeds receipt limit")
            output.extend(data[8 : 8 + length])
            data = data[8 + length :]
        return bytes(output)

    def result(self, name):
        value = self.inspect(name)
        if value["State"]["Running"]:
            return None
        if value["State"].get("OOMKilled"):
            return {
                "exit_code": value["State"]["ExitCode"],
                "output": "Memory limit exceeded",
                "timed_out": False,
                "truncated": False,
                "resource_limit": "memory",
            }
        try:
            receipt = json.loads(self.logs(name))
            if set(receipt) != {"output", "exit_code", "timed_out", "truncated"}:
                raise ValueError("Unexpected runner result")
            return receipt
        except (ValueError, TypeError):
            return {
                "exit_code": value["State"]["ExitCode"],
                "output": "Container stopped without a complete runner receipt",
                "timed_out": False,
                "truncated": False,
            }

    def initialize(self):
        version = self.api("GET", "/version")
        if version["Os"] != "linux" or int(version["Version"].split(".")[0]) < 28:
            raise ValueError("Linux Docker Engine 28+ required for isolated gateway mode")
        image = self.api("GET", f"/images/{self.runner_image}/json")
        if (
            image["Config"].get("Labels", {}).get("decagent.runner.version") != "1"
            or image["Config"].get("User") != "10001:10001"
        ):
            raise ValueError("Image is not the versioned DecAgent Python runner")
        return {"engine": version["Version"], "runner_id": image["Id"], "os": version["Os"]}

    def create_workspace(self, task_id, files):
        names = self.names(task_id)
        for name in (names["workspace"], names["baseline"]):
            self.api(
                "POST",
                "/volumes/create",
                json={
                    "Name": name,
                    "Labels": {
                        "decagent.managed": "executor",
                        "decagent.task": task_id,
                        "decagent.executor.instance": self.proxy_container,
                    },
                },
            )
        self.api(
            "POST",
            "/networks/create",
            json={
                "Name": names["network"],
                "Driver": "bridge",
                "Internal": True,
                "EnableIPv6": False,
                "CheckDuplicate": True,
                "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
                "Labels": {
                    "decagent.managed": "executor",
                    "decagent.task": task_id,
                    "decagent.executor.instance": self.proxy_container,
                },
            },
        )
        self.api(
            "POST",
            f"/networks/{names['network']}/connect",
            json={
                "Container": self.proxy_container,
                "EndpointConfig": {"Aliases": ["egress-proxy"]},
            },
        )
        data = make_archive(files)
        for baseline in (False, True):
            name = f"decagent-init-{uuid4()}"
            self.create_container(
                name,
                self.config(
                    task_id,
                    ["python", "-c", "pass"],
                    baseline=baseline,
                ),
            )
            try:
                self.api(
                    "PUT",
                    f"/containers/{name}/archive",
                    params={"path": "/workspace"},
                    content=data,
                    headers={"Content-Type": "application/x-tar"},
                )
            finally:
                self.remove_container(name)

    def snapshot(self, task_id, *, baseline=False):
        name = f"decagent-read-{uuid4()}"
        config = self.config(task_id, ["python", "-c", "import time; time.sleep(60)"])
        config["HostConfig"]["LogConfig"] = {"Type": "none"}
        self.create_container(name, config)
        try:
            self.start(name)
            execution = self.api(
                "POST",
                f"/containers/{name}/exec",
                json={
                    "AttachStdout": True,
                    "AttachStderr": False,
                    "Cmd": [
                        "python",
                        "/opt/runner/snapshot.py",
                        "baseline" if baseline else "workspace",
                    ],
                },
            )
            # Exec output is transported directly, not forwarded to the container logger.
            data = self.api(
                "POST", f"/exec/{execution['Id']}/start", json={"Detach": False, "Tty": False}
            )
            result = self.api("GET", f"/exec/{execution['Id']}/json")
            if result["Running"] or result["ExitCode"] != 0:
                raise ValueError("Source snapshot rejected a link, special file, or size limit")
            return read_archive(self.demultiplex(data, maximum=75 * 1024 * 1024))
        finally:
            self.remove_container(name)

    def write(self, task_id, path, content):
        command = json.dumps({"path": path, "content_base64": base64.b64encode(content).decode()})
        name = f"decagent-write-{uuid4()}"
        self.create_container(
            name,
            self.config(
                task_id,
                ["python", "/opt/runner/workspace.py", command],
            ),
        )
        try:
            self.start(name)
            deadline = time.monotonic() + 10
            while self.inspect(name)["State"]["Running"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Workspace write timed out")
                time.sleep(0.02)
            if self.inspect(name)["State"]["ExitCode"] != 0:
                raise ValueError("Workspace write rejected a link or special file")
        finally:
            self.remove_container(name)

    def control_targets(self):
        # Enumerate actual broker/DB listening addresses, not only synthetic private IPs.
        result = []
        for name in self.control_containers:
            value = self.inspect(name)
            ports = [
                int(port.split("/")[0])
                for port in value["Config"].get("ExposedPorts", {})
                if port.endswith("/tcp")
            ]
            for network in value["NetworkSettings"]["Networks"].values():
                for port in ports:
                    if network["IPAddress"]:
                        result.append({"host": network["IPAddress"], "port": port})
        return result

    def boundary_probe(self, task_id):
        name = f"decagent-probe-{uuid4()}"
        config = self.config(
            task_id,
            ["python", "/opt/runner/boundary_probe.py"],
            network=self.names(task_id)["network"],
        )
        config["Env"].append("BOUNDARY_CONTROL_TARGETS=" + json.dumps(self.control_targets()))
        self.create_container(name, config)
        try:
            self.start(name)
            deadline = time.monotonic() + 75
            while self.inspect(name)["State"]["Running"]:
                if time.monotonic() > deadline:
                    raise TimeoutError("Execution boundary probe timed out")
                time.sleep(0.1)
            result = json.loads(self.logs(name))
            if result.get("passed") is not True:
                raise ValueError("Execution boundary self-test failed: " + json.dumps(result))
            return result
        finally:
            self.remove_container(name)

    def remove_workspace(self, task_id):
        """Only explicit transient test/admin callers delete their own named resources."""
        names = self.names(task_id)
        with contextlib.suppress(httpx.HTTPStatusError):
            self.api(
                "POST",
                f"/networks/{names['network']}/disconnect",
                json={
                    "Container": self.proxy_container,
                    "Force": True,
                },
            )
        self.api("DELETE", f"/networks/{names['network']}")
        for name in (names["workspace"], names["baseline"]):
            self.api("DELETE", f"/volumes/{name}")
