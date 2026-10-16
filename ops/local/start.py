"""Run Windows control roles beside the Compose database and repair executor."""

import os
import sys
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

SECRET_FILES = {
    "DECAGENT_MAIN_API_KEY": "main_api_key",
    "DECAGENT_JEV_API_KEY": "jev_api_key",
    "DECAGENT_GITHUB_TOKEN": "github_token",
    "DECAGENT_BROKER_TOKEN": "broker_token",
}


def _port(name, default):
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= 65535:
        raise ValueError("Invalid local service port: " + name)
    return value


def load_local_environment(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    load_dotenv(root / ".env", override=False)
    secrets = Path(os.getenv("DECAGENT_SECRETS_DIR", ".decagent/secrets"))
    if not secrets.is_absolute():
        secrets = root / secrets
    port = _port("DECAGENT_LOCAL_POSTGRES_PORT", 55432)
    database_url = os.getenv("DECAGENT_LOCAL_DATABASE_URL")
    if not database_url:
        password = (secrets / "postgres_password").read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError("Local PostgreSQL password file is empty")
        database_url = "postgresql://decagent:" + quote(password, safe="")
        database_url += "@127.0.0.1:" + str(port) + "/decagent"
    os.environ["DECAGENT_DATABASE_URL"] = database_url
    directory = Path(os.getenv("DECAGENT_LOCAL_DATA_DIR", ".decagent/local-control"))
    os.environ["DECAGENT_DATA_DIR"] = str((root / directory).resolve())
    mcp_port = _port("DECAGENT_LOCAL_MCP_PORT", 8001)
    broker_port = _port("DECAGENT_LOCAL_BROKER_PORT", 8002)
    if mcp_port == broker_port:
        raise ValueError("Local MCP and repair broker ports must differ")
    os.environ["DECAGENT_TICKETS_MCP_URL"] = "http://127.0.0.1:" + str(mcp_port) + "/mcp"
    os.environ["DECAGENT_BROKER_URL"] = os.getenv(
        "DECAGENT_LOCAL_BROKER_URL"
    ) or "http://127.0.0.1:" + str(broker_port)
    for name, filename in SECRET_FILES.items():
        path = secrets / filename
        if not os.getenv(name) and path.is_file():
            os.environ[name] = path.read_text(encoding="utf-8").strip()
    if not os.getenv("PYTHONUTF8"):
        os.environ["PYTHONUTF8"] = "1"


def main(argv=None):
    load_local_environment()
    from decagent.cli import main as administrative

    administrative(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    main()
