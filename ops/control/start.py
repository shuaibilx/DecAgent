"""Read control-only secret files, then dispatch the regular administrative entrypoints."""

import os
import sys
from pathlib import Path
from urllib.parse import quote

SECRETS = {
    "DECAGENT_BROKER_TOKEN": "broker_token",
    "DECAGENT_GITHUB_TOKEN": "github_token",
    "DECAGENT_MAIN_API_KEY": "main_api_key",
    "DECAGENT_JEV_API_KEY": "jev_api_key",
}


def load_secret_files():
    for name, filename in SECRETS.items():
        path = Path(os.getenv(name + "_FILE", "/run/secrets/" + filename))
        if name not in os.environ and path.is_file():
            os.environ[name] = path.read_text(encoding="utf-8").strip()
    if not os.getenv("DECAGENT_DATABASE_URL"):
        path = Path(os.getenv("DECAGENT_POSTGRES_PASSWORD_FILE", "/run/secrets/postgres_password"))
        password = path.read_text(encoding="utf-8").strip()
        os.environ["DECAGENT_DATABASE_URL"] = (
            "postgresql://decagent:" + quote(password, safe="") + "@postgres/decagent"
        )


def main():
    load_secret_files()
    from decagent.cli import main as administrative

    role = sys.argv[1:]
    if role == ["api"]:
        administrative(["serve", "--host", "0.0.0.0", "--port", "8000"])
    elif role == ["sop-bootstrap"]:
        administrative(["plugins", "install-tickets"])
    elif len(role) >= 1 and role[0] == "readiness":
        from decagent.business.readiness import main as readiness_main

        return readiness_main(role[1:])
    else:
        administrative(role)


if __name__ == "__main__":
    sys.exit(main() or 0)
