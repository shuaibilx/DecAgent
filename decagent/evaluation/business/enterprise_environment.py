"""Isolated EnterpriseOps-Gym wire boundary, without a replacement task scorer.

Each context creates one digest-pinned container and a fresh upstream database.
The snapshot and original scorer configuration stay on the evaluator side. The
model receives only original prompts and the selected/restricted MCP catalog.
Live-state fingerprints and the evaluator-only plan session bind paired trials.
Only the original task verifier can establish an official task result; container
or synthetic connectivity checks are infrastructure evidence.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import httpx

from decagent.decisions.contracts import canonical_json, digest

OWNER_LABEL = "io.decagent.enterpriseops.trial"
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STATE_ROWS_PER_TABLE = 10_000
_STATE_BYTES_PER_TABLE = 16 * 1024 * 1024


def _docker(args: list[str]) -> str:
    return subprocess.run(
        args, check=True, text=True, capture_output=True, timeout=120,
    ).stdout.strip()


class OwnedEnterpriseContainer:
    """No volumes, ambient ports, image pulls, or deletion by name/prefix."""

    def __init__(self, image: str, *, platform: str = "linux/amd64", runner=_docker):
        if not _IMAGE.fullmatch(image):
            raise ValueError("EnterpriseOps image must use an immutable SHA-256 digest")
        self.image = image
        self.platform = platform
        self._run = runner
        self._owner = uuid4().hex
        self.container_id: str | None = None
        self.base_url: str | None = None

    def _inspect_owned(self) -> dict:
        records = json.loads(self._run(["docker", "inspect", self.container_id]))
        if len(records) != 1:
            raise RuntimeError("Container ownership could not be verified")
        record = records[0]
        labels = record.get("Config", {}).get("Labels") or {}
        if record.get("Id") != self.container_id or labels.get(OWNER_LABEL) != self._owner:
            raise RuntimeError("Container ownership mismatch; refusing lifecycle action")
        return record

    def __enter__(self):
        if self.container_id is not None:
            raise RuntimeError("Container context is already active")
        cid = self._run([
            "docker", "create", "--pull", "never", "--platform", self.platform,
            "--label", f"{OWNER_LABEL}={self._owner}",
            "--publish", "127.0.0.1::8005", self.image,
        ]).strip()
        if not _CONTAINER_ID.fullmatch(cid):
            raise RuntimeError("Docker did not return a complete container ID")
        self.container_id = cid
        try:
            self._inspect_owned()
            self._run(["docker", "start", cid])
            record = self._inspect_owned()
            ports = record["NetworkSettings"]["Ports"]
            bound = ports.get("8005/tcp") or []
            if len(bound) != 1 or bound[0].get("HostIp") != "127.0.0.1":
                raise RuntimeError("Container must publish exactly one loopback binding")
            if any(bindings for port, bindings in ports.items() if port != "8005/tcp"):
                raise RuntimeError("Unexpected published container port")
            host_port = int(bound[0]["HostPort"])
            if not 1 <= host_port <= 65535:
                raise RuntimeError("Invalid allocated container port")
            self.base_url = f"http://127.0.0.1:{host_port}"
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.container_id is not None:
            self._inspect_owned()
            self._run(["docker", "rm", "--force", self.container_id])
            self.container_id = None
            self.base_url = None

    def __exit__(self, *_):
        self.close()


class EnterpriseProtocolError(RuntimeError):
    """An upstream HTTP/JSON-RPC failure; never a successful tool receipt."""


class EnterpriseMCPClient:
    """Upstream JSON-RPC payloads, schema and results without argument coercion.

The upstream verifier uses base_url/database_id/context/_get_auth_headers for
its private /api/sql-runner HTTP call. Those attributes preserve that boundary;
sql-runner is deliberately not a model-facing method or tool.
"""

    def __init__(self, base_url, database_id, *, context=None, http: httpx.Client):
        self.base_url = base_url.rstrip("/")
        self.database_id = database_id
        self.context = deepcopy(context or {})
        self._http = http
        self._request_id = 0
        self._session_id = None
        self._timeout_provider = None

    def set_timeout_provider(self, provider):
        if provider is not None and not callable(provider):
            raise TypeError("An HTTP deadline provider must be callable")
        self._timeout_provider = provider

    def _effective_timeout(self, timeout_seconds=None):
        if timeout_seconds is None and self._timeout_provider is not None:
            timeout_seconds = self._timeout_provider()
        if timeout_seconds is None:
            return None
        if (
            type(timeout_seconds) not in {int, float}
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise TimeoutError("EnterpriseOps request deadline expired")
        return float(timeout_seconds)

    def _get_auth_headers(self):
        # Frozen CSM/ITSM tasks use context identities, not external credentials.
        return {}

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-database-id": self.database_id,
        }
        for key, value in self.context.items():
            name = key if key.lower().startswith("x-") else "x-" + key.lower().replace("_", "-")
            headers[name] = str(value)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _request(self, method, params, *, timeout_seconds=None):
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id,
                   "method": method, "params": params}
        timeout_seconds = self._effective_timeout(timeout_seconds)
        request = {"json": payload, "headers": self._headers()}
        if timeout_seconds is not None:
            request["timeout"] = timeout_seconds
        response = self._http.post(self.base_url + "/mcp", **request)
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            self._session_id = response.headers["mcp-session-id"]
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            events = []
            for event in response.text.replace("\r\n", "\n").split("\n\n"):
                lines = [line[5:].lstrip(" ") for line in event.splitlines()
                         if line.startswith("data:")]
                if lines:
                    events.append(json.loads("\n".join(lines)))
            matching = [event for event in events if event.get("id") == self._request_id]
            if len(matching) != 1:
                raise EnterpriseProtocolError("Missing or duplicate upstream SSE response")
            data = matching[0]
        else:
            data = response.json()
        if data.get("error") is not None:
            raise EnterpriseProtocolError(str(data["error"]))
        if data.get("jsonrpc") != "2.0" or data.get("id") != self._request_id:
            raise EnterpriseProtocolError("Mismatched upstream JSON-RPC response")
        if "result" not in data:
            raise EnterpriseProtocolError("Missing upstream JSON-RPC result")
        return data["result"]

    def initialize(self):
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "clientInfo": {"name": "standalone-benchmark-executor", "version": "1.0.0"},
        })
        response = self._http.post(self.base_url + "/mcp", headers=self._headers(), json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })
        response.raise_for_status()
        return result

    def list_tools(self):
        return self._request("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: dict, *, timeout_seconds=None):
        return self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout_seconds=timeout_seconds,
        )

    def _private_select(self, query: str) -> list[dict]:
        if type(query) is not str or not query.lstrip().upper().startswith("SELECT "):
            raise ValueError("Private evaluation query must be a read-only SELECT")
        try:
            timeout_seconds = self._effective_timeout()
            request = {
                "json": {"query": query, "database_id": self.database_id},
                "headers": self._headers(),
            }
            if timeout_seconds is not None:
                request["timeout"] = timeout_seconds
            response = self._http.post(
                self.base_url + "/api/sql-runner",
                **request,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EnterpriseProtocolError("Private database state query failed") from exc
        if (
            type(result) is not dict
            or result.get("success") is not True
            or type(result.get("data")) is not list
            or type(result.get("row_count")) is not int
            or result["row_count"] != len(result["data"])
            or any(type(row) is not dict for row in result["data"])
        ):
            raise EnterpriseProtocolError("Private database state query returned invalid rows")
        return result["data"]

    def state_fingerprint(self) -> dict:
        """Hash a bounded, deterministic projection of the live SQLite database."""
        schema = self._private_select(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type,name LIMIT 257"
        )
        table_definitions = [row for row in schema if row.get("type") == "table"]
        schema_bytes = sum(len(canonical_json(row).encode("utf-8")) for row in schema)
        if (
            not table_definitions
            or len(schema) > 256
            or schema_bytes > 4 * 1024 * 1024
            or len(table_definitions) > 128
        ):
            raise EnterpriseProtocolError("Private database table count is outside its bound")
        tables, total_rows, total_bytes = [], 0, 0
        for definition in table_definitions:
            name, create_sql = definition.get("name"), definition.get("sql")
            if (
                type(name) is not str
                or not _SQLITE_IDENTIFIER.fullmatch(name)
                or type(create_sql) is not str
                or "WITHOUT ROWID" in create_sql.upper()
            ):
                raise EnterpriseProtocolError("Private database schema cannot be fingerprinted")
            rows = self._private_select(
                f'SELECT * FROM "{name}" ORDER BY rowid LIMIT {_STATE_ROWS_PER_TABLE + 1}'
            )
            if len(rows) > _STATE_ROWS_PER_TABLE:
                raise EnterpriseProtocolError("Private database table exceeds its row bound")
            encoded_rows = [canonical_json(row) for row in rows]
            row_bytes = sum(len(row.encode("utf-8")) for row in encoded_rows)
            total_bytes += row_bytes
            if row_bytes > _STATE_BYTES_PER_TABLE or total_bytes > 64 * 1024 * 1024:
                raise EnterpriseProtocolError("Private database state exceeds its byte bound")
            tables.append({"name": name, "rows": encoded_rows})
            total_rows += len(rows)
        return {
            "digest": digest({"schema": schema, "tables": tables}),
            "table_count": len(tables),
            "row_count": total_rows,
        }


@dataclass(frozen=True)
class EnterpriseTrialSpec:
    """Evaluator-owned input; never serialize this object into a model prompt."""

    task_id: str
    domain: str
    image: str
    platform: str
    snapshot_path: Path = field(repr=False)
    snapshot_sha256: str
    system_prompt: str = field(repr=False)
    user_prompt: str = field(repr=False)
    selected_tools: tuple[str, ...]
    restricted_tools: tuple[str, ...]
    server_name: str
    context: dict = field(repr=False)
    verifiers: tuple[dict, ...] = field(repr=False)


def spec_from_pinned_row(root: Path, row: dict, source_manifest: dict) -> EnterpriseTrialSpec:
    """Resolve an unchanged cached task against the pinned image/snapshot list.

    The caller must first validate source_manifest.json and the Parquet/inventory
    hashes with the repository provenance gate; accepting a caller-supplied row
    is not a substitute for that gate or proof of a frozen official split.
    """
    domain = row["domain"]
    if domain not in {"csm", "itsm"}:
        raise ValueError("Only pinned CSM/ITSM single-domain trials are supported")
    upstream = source_manifest["enterpriseops_gym"]
    environment = upstream["mcp_environment"]
    if environment["endpoint"] != "/mcp" or row.get("mcp_endpoint", "/mcp") != "/mcp":
        raise ValueError("Unsupported upstream MCP endpoint")
    servers = row["gym_servers_config"]
    servers = json.loads(servers) if isinstance(servers, str) else deepcopy(servers)
    if len(servers) != 1 or servers[0].get("auth_config"):
        raise ValueError("Expected one context-identity gym without external credentials")
    server = servers[0]
    seed = PurePosixPath(server["seed_database_file"].replace("\\", "/"))
    if seed.is_absolute() or ".." in seed.parts or ":" in str(seed):
        raise ValueError("Unsafe upstream snapshot path")
    records = [record for record in upstream["database_snapshots"]["referenced_snapshot_sha256"]
               if record["domain"] == domain
               and (record["path"] == str(seed) or record["path"].endswith("/" + str(seed)))]
    if len(records) != 1:
        raise ValueError("Task snapshot is absent or ambiguous in the pinned manifest")
    record = records[0]
    snapshot = (Path(root) / record["path"]).resolve()
    if not snapshot.is_relative_to(Path(root).resolve()):
        raise ValueError("Pinned snapshot escapes the benchmark root")
    images = [image for image in environment["images"]
              if image["image"].endswith("/enterpriseops-gym-mcp-" + domain)]
    if len(images) != 1:
        raise ValueError("Domain image is absent or ambiguous in the pinned manifest")
    image = images[0]
    verifiers = row["verifiers"]
    verifiers = json.loads(verifiers) if isinstance(verifiers, str) else deepcopy(verifiers)
    return EnterpriseTrialSpec(
        task_id=row["task_id"], domain=domain,
        image=image["image"] + "@" + image["manifest_digest"],
        platform=image["platform"], snapshot_path=snapshot,
        snapshot_sha256=record["sha256"], system_prompt=row["system_prompt"],
        user_prompt=row["user_prompt"], selected_tools=tuple(row.get("selected_tools") or ()),
        restricted_tools=tuple(row.get("restricted_tools") or ()),
        server_name=server["mcp_server_name"], context=deepcopy(server.get("context") or {}),
        verifiers=tuple(verifiers),
    )


class EnterpriseEnvironment:
    """Fresh container and official snapshot initialization for every context.

Reset means discard the entire prior container and create another context.
There is no in-place reset claim or cross-trial memory/cache here. Upstream
HTTP error bodies, SQL and verifier results must stay in evaluator diagnostics.
"""

    def __init__(self, spec: EnterpriseTrialSpec, *, runner=_docker, http=None,
                 ready_timeout: float = 60):
        self._spec = spec
        self._runner = runner
        self._http = http
        self._owns_http = http is None
        self._container = None
        self._client = None
        self._tools = []
        self._ready_timeout = ready_timeout
        self.database_id = None
        self._cleanup_verified = False

    def _wait_ready(self):
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            try:
                response = self._http.get(self._container.base_url + "/", timeout=2)
                if response.status_code < 500:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise TimeoutError("EnterpriseOps container did not become HTTP-ready")

    def __enter__(self):
        if self._container is not None:
            raise RuntimeError("Trial context is already active")
        self._cleanup_verified = False
        sql_bytes = self._spec.snapshot_path.read_bytes()
        if sha256(sql_bytes).hexdigest() != self._spec.snapshot_sha256:
            raise ValueError("Pinned EnterpriseOps snapshot SHA-256 mismatch")
        self._container = OwnedEnterpriseContainer(
            self._spec.image, platform=self._spec.platform, runner=self._runner,
        )
        if self._owns_http:
            self._http = httpx.Client(timeout=30, trust_env=False)
        try:
            self._container.__enter__()
            self._wait_ready()
            self.database_id = "db_" + str(int(time.time() * 1000)) + "_" + uuid4().hex[:9]
            response = self._http.post(
                self._container.base_url + "/api/seed-database", timeout=1200,
                json={"database_id": self.database_id, "name": "DecAgent isolated trial",
                      "description": "Fresh pinned upstream snapshot",
                      "sql_content": sql_bytes.decode("utf-8")},
            )
            response.raise_for_status()
            self._client = EnterpriseMCPClient(
                self._container.base_url, self.database_id,
                context=self._spec.context, http=self._http,
            )
            self._client.initialize()
            tools = self._client.list_tools()
            selected = set(self._spec.selected_tools)
            restricted = set(self._spec.restricted_tools)
            self._tools = [deepcopy(tool) for tool in tools
                           if (not selected or tool["name"] in selected)
                           and tool["name"] not in restricted]
            return self
        except BaseException:
            self.close()
            raise

    def _active(self):
        if self._client is None:
            raise RuntimeError("EnterpriseOps trial is not active")

    def model_input(self):
        self._active()
        return {"system_prompt": self._spec.system_prompt,
                "user_prompt": self._spec.user_prompt, "tools": deepcopy(self._tools)}

    @property
    def spec(self):
        self._active()
        return self._spec

    @property
    def cleanup_verified(self):
        return (
            self._cleanup_verified
            and self._container is None
            and self.database_id is None
            and self._client is None
        )

    def call_tool(self, name: str, arguments: dict, *, timeout_seconds=None):
        self._active()
        if name not in {tool["name"] for tool in self._tools}:
            raise ValueError("Tool is not available in the original task catalog")
        return self._client.call_tool(
            name, arguments, timeout_seconds=timeout_seconds
        )

    def set_deadline_provider(self, provider):
        self._active()
        self._client.set_timeout_provider(provider)

    def scorer_hook(self):
        self._active()
        return UpstreamScorerHook(self._client, self._spec.server_name)

    def state_fingerprint(self) -> dict:
        self._active()
        return self._client.state_fingerprint()

    def close(self):
        container_closed = False
        http_closed = not self._owns_http or self._http is None
        try:
            if self._container is not None:
                self._container.close()
            # Keep the exact ownership handle on a failed inspect/remove so a
            # caller can retry checked cleanup rather than silently leaking it.
            self._container = None
            self.database_id = None
            container_closed = True
        finally:
            try:
                if self._owns_http and self._http is not None:
                    self._http.close()
                http_closed = True
            finally:
                self._client = None
                self._tools = []
                self._cleanup_verified = (
                    container_closed
                    and http_closed
                    and self._container is None
                    and self.database_id is None
                )

    def __exit__(self, *_):
        self.close()


class UpstreamScorerHook:
    """Private evaluator port accepting the ORIGINAL VerifierEngine/configs.

The harness creates the upstream engine with ``{server_name: hook.client}``.
This hook delegates all SQL extraction, expected-value comparisons and judge
semantics to that engine, unchanged. It never calls a model by itself. Supplying
an LLM judge to the upstream engine requires independent budget authorization.
"""

    def __init__(self, client, server_name: str):
        self.client = client
        self.server_name = server_name

    async def run(self, engine, original_configs, model_response: dict) -> list[Any]:
        results = []
        for config in original_configs:
            if config.gym_name and config.gym_name != self.server_name:
                raise ValueError("Verifier gym differs from this isolated trial")
            results.append(await engine.execute_verifier(
                config, model_response, self.client.database_id,
                context=self.client.context, gym_name=config.gym_name,
            ))
        return results
