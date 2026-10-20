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
    main_provider: str = "primary"
    main_price: TokenPrice | None = None
    fallback_price: TokenPrice | None = None
    unknown_main_reservation: Decimal | None = None
    unknown_fallback_reservation: Decimal | None = None
    main_fallback_enabled: bool = False
    max_primary_attempts: int = 2
    max_retry_after_seconds: int = 30
    max_model_input_tokens: int = 65_536
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
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.main_provider):
            raise ValueError("Invalid primary provider identifier")
        if type(self.main_fallback_enabled) is not bool:
            raise ValueError("Primary fallback must be a boolean")
        if (
            type(self.max_primary_attempts) is not int
            or not 1 <= self.max_primary_attempts <= 2
        ):
            raise ValueError("Primary attempts must be between one and two")
        if (
            type(self.max_retry_after_seconds) is not int
            or not 0 <= self.max_retry_after_seconds <= 30
        ):
            raise ValueError("Retry-After threshold must be between zero and 30 seconds")
        if (
            type(self.max_model_input_tokens) is not int
            or not 1 <= self.max_model_input_tokens <= 65_536
        ):
            raise ValueError("Model input limit must be between one and 65,536")
        if type(self.context_tokens) is not int or self.context_tokens <= 0:
            raise ValueError("Context token limit must be a positive integer")
        if self.runner_digest and not re.fullmatch(r"sha256:[a-f0-9]{64}", self.runner_digest):
            raise ValueError("A fixed runner image digest is required")
        for value in (
            self.jev_reservation,
            self.unknown_main_reservation,
            self.unknown_fallback_reservation,
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("Fee reservations must be positive and finite")
        if self.main_fallback_enabled and not (
            self.fallback_price or self.unknown_fallback_reservation
        ):
            raise ValueError("Fallback requires versioned pricing or a bounded fee reservation")

    @classmethod
    def from_env(cls):
        def read_price(prefix):
            version = os.getenv(f"DECAGENT_{prefix}_PRICE_VERSION", "")
            if not version:
                return None
            return TokenPrice(
                version,
                Decimal(os.environ[f"DECAGENT_{prefix}_INPUT_PER_MILLION"]),
                Decimal(os.environ[f"DECAGENT_{prefix}_OUTPUT_PER_MILLION"]),
            )

        price = read_price("MAIN")
        fallback_price = read_price("MAIN_FALLBACK")
        unknown = os.getenv("DECAGENT_MAIN_UNKNOWN_RESERVATION", "")
        unknown_fallback = os.getenv("DECAGENT_MAIN_FALLBACK_UNKNOWN_RESERVATION", "")
        calibration = os.getenv("DECAGENT_CALIBRATION_FILE", "")
        conditional = os.getenv("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED", "false").strip().lower()
        fallback_enabled = os.getenv("DECAGENT_MAIN_FALLBACK_ENABLED", "false").strip().lower()
        runner_reference = os.getenv("DECAGENT_RUNNER_IMAGE", "")
        runner_digest = runner_reference
        runner_match = re.fullmatch(r"[^@\s]+@(sha256:[a-f0-9]{64})", runner_reference)
        if runner_match:
            runner_digest = runner_match.group(1)
        if conditional not in {"true", "false"}:
            raise ValueError("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED must be true or false")
        if fallback_enabled not in {"true", "false"}:
            raise ValueError("DECAGENT_MAIN_FALLBACK_ENABLED must be true or false")
        return cls(
            broker_url=os.getenv("DECAGENT_BROKER_URL", "http://127.0.0.1:8002"),
            tickets_mcp_url=os.getenv("DECAGENT_TICKETS_MCP_URL", "http://127.0.0.1:8001/mcp"),
            broker_token=os.getenv("DECAGENT_BROKER_TOKEN", ""),
            runner_digest=runner_digest,
            author_name=os.getenv("DECAGENT_GIT_AUTHOR_NAME", "DecAgent"),
            author_email=os.getenv("DECAGENT_GIT_AUTHOR_EMAIL", ""),
            strategy=os.getenv("DECAGENT_DECISION_STRATEGY", "jev"),
            calibrations=Path(calibration) if calibration else None,
            main_provider=os.getenv("DECAGENT_MAIN_PROVIDER", "primary"),
            main_price=price,
            fallback_price=fallback_price,
            unknown_main_reservation=Decimal(unknown) if unknown else None,
            unknown_fallback_reservation=Decimal(unknown_fallback) if unknown_fallback else None,
            main_fallback_enabled=fallback_enabled == "true",
            max_primary_attempts=int(os.getenv("DECAGENT_MAIN_MAX_ATTEMPTS", "2")),
            max_retry_after_seconds=int(
                os.getenv("DECAGENT_MAIN_RETRY_AFTER_THRESHOLD", "30")
            ),
            max_model_input_tokens=int(os.getenv("DECAGENT_MODEL_MAX_INPUT_TOKENS", "65536")),
            jev_reservation=Decimal(os.getenv("DECAGENT_JEV_RESERVATION", "0.01")),
            context_tokens=int(os.getenv("DECAGENT_CONTEXT_TOKENS", "65536")),
            conditional_workflows_enabled=conditional == "true",
        )
