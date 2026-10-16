"""Instance execution and price configuration, kept outside task parameters."""

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .model_calls import TokenPrice


@dataclass(frozen=True)
class RuntimeConfig:
    broker_url: str = "http://127.0.0.1:8002"
    tickets_mcp_url: str = "http://127.0.0.1:8001/mcp"
    broker_token: str = field(default="", repr=False)
    runner_digest: str = ""
    author_name: str = "DecAgent"
    author_email: str = ""
    strategy: str = "jev"
    calibrations: Path | None = None
    main_price: TokenPrice | None = None
    unknown_main_reservation: Decimal | None = None
    jev_reservation: Decimal = Decimal("0.01")
    context_tokens: int = 65536
    conditional_workflows_enabled: bool = False

    def __post_init__(self):
        if type(self.conditional_workflows_enabled) is not bool:
            raise ValueError("Conditional workflows entry flag must be a boolean")
        url = urlsplit(self.broker_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or (url.username or url.password or url.query or url.fragment)
        ):
            raise ValueError("Invalid internal broker URL")
        mcp_url = urlsplit(self.tickets_mcp_url)
        if (
            mcp_url.scheme not in {"http", "https"}
            or not mcp_url.hostname
            or mcp_url.path != "/mcp"
            or mcp_url.username
            or mcp_url.password
            or mcp_url.query
            or mcp_url.fragment
        ):
            raise ValueError("Invalid trusted business MCP URL")
        if self.strategy not in {"rule", "jev"}:
            raise ValueError("Runtime strategy must be rule or jev")
        if type(self.context_tokens) is not int or self.context_tokens <= 0:
            raise ValueError("Context token limit must be a positive integer")
        if self.runner_digest and not re.fullmatch(r"sha256:[a-f0-9]{64}", self.runner_digest):
            raise ValueError("A fixed runner image digest is required")
        for value in (self.jev_reservation, self.unknown_main_reservation):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("Fee reservations must be positive and finite")

    @classmethod
    def from_env(cls):
        price_version = os.getenv("DECAGENT_MAIN_PRICE_VERSION", "")
        price = None
        if price_version:
            price = TokenPrice(
                price_version,
                Decimal(os.environ["DECAGENT_MAIN_INPUT_PER_MILLION"]),
                Decimal(os.environ["DECAGENT_MAIN_OUTPUT_PER_MILLION"]),
            )
        unknown = os.getenv("DECAGENT_MAIN_UNKNOWN_RESERVATION", "")
        calibration = os.getenv("DECAGENT_CALIBRATION_FILE", "")
        conditional = os.getenv("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED", "false").strip().lower()
        if conditional not in {"true", "false"}:
            raise ValueError("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED must be true or false")
        return cls(
            broker_url=os.getenv("DECAGENT_BROKER_URL", "http://127.0.0.1:8002"),
            tickets_mcp_url=os.getenv("DECAGENT_TICKETS_MCP_URL", "http://127.0.0.1:8001/mcp"),
            broker_token=os.getenv("DECAGENT_BROKER_TOKEN", ""),
            runner_digest=os.getenv("DECAGENT_RUNNER_IMAGE", ""),
            author_name=os.getenv("DECAGENT_GIT_AUTHOR_NAME", "DecAgent"),
            author_email=os.getenv("DECAGENT_GIT_AUTHOR_EMAIL", ""),
            strategy=os.getenv("DECAGENT_DECISION_STRATEGY", "jev"),
            calibrations=Path(calibration) if calibration else None,
            main_price=price,
            unknown_main_reservation=Decimal(unknown) if unknown else None,
            jev_reservation=Decimal(os.getenv("DECAGENT_JEV_RESERVATION", "0.01")),
            context_tokens=int(os.getenv("DECAGENT_CONTEXT_TOKENS", "65536")),
            conditional_workflows_enabled=conditional == "true",
        )
