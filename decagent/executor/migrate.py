"""Explicit Compose migration job, independent from broker service startup."""

from pathlib import Path
from urllib.parse import quote

from decagent.storage.database import Database

if __name__ == "__main__":
    password = Path("/run/secrets/postgres_password").read_text().strip()
    Database(f"postgresql://decagent:{quote(password, safe='')}@postgres/decagent").migrate()
