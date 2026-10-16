"""Control-service configuration; never forward this object to a task executor."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="", repr=False)
    data_dir: Path = Path(".decagent")
    public_url: str = "http://127.0.0.1:8000"
    main_model: str = "gpt-4.1"
    main_base_url: str = "https://api.openai.com/v1"
    main_api_key: str = field(default="", repr=False)
    jev_model: str = "jev-1.13.0"
    jev_provider: str = "typesafe"
    jev_api_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    graph_version: str = "1"

    def __post_init__(self):
        if self.jev_provider not in {"typesafe", "openrouter"}:
            raise ValueError("DECAGENT_JEV_PROVIDER must be typesafe or openrouter")
        path = Path(self.data_dir).expanduser().resolve()
        if ".waku" in {part.casefold() for part in path.parts}:
            raise ValueError("DecAgent data must be separate from .waku")
        object.__setattr__(self, "data_dir", path)
        url = urlsplit(self.public_url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("Remote workbench URLs require HTTPS")

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env", override=False)
        return cls(
            database_url=os.getenv("DECAGENT_DATABASE_URL", ""),
            data_dir=Path(os.getenv("DECAGENT_DATA_DIR", ".decagent")),
            public_url=os.getenv("DECAGENT_PUBLIC_URL", "http://127.0.0.1:8000"),
            main_model=os.getenv("DECAGENT_MAIN_MODEL", "gpt-4.1"),
            main_base_url=os.getenv("DECAGENT_MAIN_BASE_URL", "https://api.openai.com/v1"),
            main_api_key=os.getenv("DECAGENT_MAIN_API_KEY", ""),
            jev_model=os.getenv("DECAGENT_JEV_MODEL", "jev-1.13.0"),
            jev_provider=os.getenv("DECAGENT_JEV_PROVIDER", "typesafe"),
            jev_api_key=os.getenv("DECAGENT_JEV_API_KEY", ""),
            github_token=os.getenv("DECAGENT_GITHUB_TOKEN", ""),
        )
