"""Build the real wheel and inspect resource availability outside the checkout."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path


def test_plugin_wheel_contains_runtime_resources(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
        cwd=root,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    wheel = next(tmp_path.glob("*.whl"))
    required = {
        "decagent/business/tickets/manifest.json",
        "decagent/business/tickets/SKILL.md",
        "decagent/business/mcp_server.py",
        "decagent/business/mcp_client.py",
        "decagent/storage/011_business_mcp_tasks.sql",
        "decagent/storage/006_service_tickets.sql",
    }
    with zipfile.ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())
        assert "decagent/business/tickets/cli.py" not in archive.namelist()
        assert "decagent/business/runner.py" not in archive.namelist()
        manifest = json.loads(archive.read("decagent/business/tickets/manifest.json"))
        assert manifest["id"] == "service-tickets" and manifest["level"] == "enhanced"
        skill = archive.read("decagent/business/tickets/SKILL.md").decode("utf-8")
        assert "operation_id" in skill
        archive.extractall(tmp_path / "installed")
    # Read with importlib.resources from extracted wheel while outside source checkout.
    script = """import importlib.resources,json,sys
sys.path.insert(0,sys.argv[1])
from decagent.business.tickets import builtin_manifest
assert builtin_manifest()['id']=='service-tickets'
assert importlib.resources.files('decagent.business.tickets').joinpath('SKILL.md').is_file()
assert importlib.resources.files('decagent.storage').joinpath('006_service_tickets.sql').is_file()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "installed")],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
