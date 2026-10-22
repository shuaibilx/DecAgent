"""Read-only database and MCP checks for the business readiness endpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

import psycopg

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from decagent.storage.database import SCHEMA_VERSION


def _health_url(mcp_url):
    parts = urlsplit(mcp_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.path != "/mcp"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        return None
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))


def _mcp_ready(mcp_url):
    health_url = _health_url(mcp_url)
    if health_url is None:
        return False
    try:
        with urlopen(health_url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("service") == "service-tickets"
        and payload.get("ready") is True
    )


def business_readiness(database, runtime_config):
    """Check migrations, the registered business catalog, MCP and feature profile."""
    components = {
        "database": False,
        "business_plugin": False,
        "mcp": False,
        "conditional_workflows": runtime_config.conditional_workflows_enabled is True,
    }
    try:
        spec = PluginSpec.from_mapping(builtin_manifest())
        with database.connect() as conn:
            versions = conn.execute(
                "SELECT version FROM app.schema_migrations ORDER BY version"
            ).fetchall()
            components["database"] = [row["version"] for row in versions] == list(
                range(1, SCHEMA_VERSION + 1)
            )
            plugin = conn.execute(
                "SELECT enabled,manifest_digest FROM app.business_scopes WHERE plugin_id=%s",
                (spec.id,),
            ).fetchone()
            components["business_plugin"] = bool(
                plugin
                and plugin["enabled"] is True
                and plugin["manifest_digest"] == spec.digest
            )
    except (psycopg.Error, KeyError, TypeError, ValueError):
        pass
    try:
        components["mcp"] = _mcp_ready(runtime_config.tickets_mcp_url) is True
    except (OSError, RuntimeError, TimeoutError, ValueError):
        components["mcp"] = False
    return {
        "status": "ready" if all(components.values()) else "not_ready",
        "components": components,
    }


def is_business_worker_commandline(commandline):
    """Return whether a Linux PID 1 argv belongs to the isolated business worker."""
    return isinstance(commandline, (list, tuple)) and len(commandline) >= 3 and list(
        commandline[-3:]
    ) == ["worker", "--kind", "business"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("api", "worker"), required=True)
    args = parser.parse_args(argv)
    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig
    from decagent.storage.database import Database

    settings = Settings.from_env()
    runtime = RuntimeConfig.from_env()
    report = business_readiness(Database(settings.database_url), runtime)
    if args.role == "worker":
        try:
            commandline = Path("/proc/1/cmdline").read_bytes().decode("utf-8").rstrip("\0")
            argv0 = commandline.split("\0")
        except (OSError, UnicodeError):
            argv0 = []
        report["components"]["business_worker_process"] = is_business_worker_commandline(
            argv0
        )
        report["status"] = (
            "ready" if all(report["components"].values()) else "not_ready"
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ready" else 1
