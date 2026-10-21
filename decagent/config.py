"""Control-service configuration; never forward this object to a task executor."""

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
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
    fallback_model: str = "deepseek/deepseek-v4.1-flash"
    fallback_base_url: str = "https://openrouter.ai/api/v1"
    fallback_api_key: str = field(default="", repr=False)
    fallback_enabled: bool = False
    guest_trial_enabled: bool = False
    guest_trial_daily_cost_usd: Decimal = Decimal("5.000000")
    guest_trial_daily_qwen_tokens: int = 5_000_000
    guest_trial_daily_tasks_per_guest: int = 30
    guest_trial_daily_qwen_tokens_per_guest: int = 500_000
    guest_trial_fx_source_date: date | None = None
    guest_trial_fx_cny_per_usd: Decimal | None = None
    guest_trial_fx_source_url: str | None = None
    guest_trial_fx_safety_margin_bps: int = 1000
    jev_model: str = "jev-1.13.0"
    jev_provider: str = "typesafe"
    jev_api_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    graph_version: str = "1"

    def __post_init__(self):
        if self.jev_provider not in {"typesafe", "openrouter"}:
            raise ValueError("DECAGENT_JEV_PROVIDER must be typesafe or openrouter")
        if type(self.fallback_enabled) is not bool:
            raise ValueError("DECAGENT_MAIN_FALLBACK_ENABLED must be a boolean")
        if type(self.guest_trial_enabled) is not bool:
            raise ValueError("DECAGENT_GUEST_TRIAL_ENABLED must be a boolean")
        try:
            daily_cost = Decimal(str(self.guest_trial_daily_cost_usd))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("DECAGENT_GUEST_TRIAL_DAILY_COST_USD must be a decimal") from exc
        if not daily_cost.is_finite() or daily_cost <= 0:
            raise ValueError("DECAGENT_GUEST_TRIAL_DAILY_COST_USD must be positive")
        object.__setattr__(self, "guest_trial_daily_cost_usd", daily_cost)
        for name in (
            "guest_trial_daily_qwen_tokens",
            "guest_trial_daily_tasks_per_guest",
            "guest_trial_daily_qwen_tokens_per_guest",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        fx_values = (
            self.guest_trial_fx_source_date,
            self.guest_trial_fx_cny_per_usd,
            self.guest_trial_fx_source_url,
        )
        if any(value is not None for value in fx_values):
            if not all(value is not None for value in fx_values):
                raise ValueError("Guest trial FX date, rate, and source URL must be set together")
            if type(self.guest_trial_fx_source_date) is not date:
                raise ValueError("Guest trial FX source date must be a date")
            try:
                fx_rate = Decimal(str(self.guest_trial_fx_cny_per_usd))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("Guest trial FX rate must be a decimal") from exc
            if not fx_rate.is_finite() or fx_rate <= 0:
                raise ValueError("Guest trial FX rate must be positive")
            object.__setattr__(self, "guest_trial_fx_cny_per_usd", fx_rate)
            source = urlsplit(self.guest_trial_fx_source_url)
            if (
                source.scheme != "https"
                or source.hostname != "www.chinamoney.com.cn"
                or source.username
                or source.password
                or source.fragment
            ):
                raise ValueError("Guest trial FX source must be an HTTPS CFETS URL")
        if (
            type(self.guest_trial_fx_safety_margin_bps) is not int
            or not 1000 <= self.guest_trial_fx_safety_margin_bps < 10000
        ):
            raise ValueError("Guest trial FX margin must be at least 10% and below 100%")
        if self.fallback_enabled and not self.resolved_fallback_api_key:
            raise ValueError("OpenRouter fallback requires an API key")
        fallback = urlsplit(self.fallback_base_url)
        if (
            fallback.scheme != "https"
            or fallback.hostname != "openrouter.ai"
            or fallback.port not in {None, 443}
            or fallback.username
            or fallback.password
            or fallback.path != "/api/v1"
            or fallback.query
            or fallback.fragment
        ):
            raise ValueError("Fallback must use the OpenRouter HTTPS endpoint")
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
        jev_provider = os.getenv("DECAGENT_JEV_PROVIDER", "typesafe")
        jev_api_key = os.getenv("DECAGENT_JEV_API_KEY", "")
        fallback_enabled = os.getenv("DECAGENT_MAIN_FALLBACK_ENABLED", "false").strip().lower()
        if fallback_enabled not in {"true", "false"}:
            raise ValueError("DECAGENT_MAIN_FALLBACK_ENABLED must be true or false")
        guest_trial_enabled = os.getenv("DECAGENT_GUEST_TRIAL_ENABLED", "false").strip().lower()
        if guest_trial_enabled not in {"true", "false"}:
            raise ValueError("DECAGENT_GUEST_TRIAL_ENABLED must be true or false")
        try:
            guest_trial_daily_cost_usd = Decimal(
                os.getenv("DECAGENT_GUEST_TRIAL_DAILY_COST_USD", "5.00")
            )
            guest_trial_fx_rate_text = os.getenv(
                "DECAGENT_GUEST_TRIAL_FX_CNY_PER_USD", ""
            ).strip()
            guest_trial_fx_rate = (
                Decimal(guest_trial_fx_rate_text) if guest_trial_fx_rate_text else None
            )
            guest_trial_daily_qwen_tokens = int(
                os.getenv("DECAGENT_GUEST_TRIAL_DAILY_QWEN_TOKENS", "5000000")
            )
            guest_trial_daily_tasks_per_guest = int(
                os.getenv("DECAGENT_GUEST_TRIAL_DAILY_TASKS_PER_GUEST", "30")
            )
            guest_trial_daily_qwen_tokens_per_guest = int(
                os.getenv("DECAGENT_GUEST_TRIAL_DAILY_QWEN_TOKENS_PER_GUEST", "500000")
            )
            guest_trial_fx_margin_bps = int(
                os.getenv("DECAGENT_GUEST_TRIAL_FX_SAFETY_MARGIN_BPS", "1000")
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Guest trial budget settings contain an invalid number") from exc
        guest_trial_fx_source_date_text = os.getenv(
            "DECAGENT_GUEST_TRIAL_FX_SOURCE_DATE", ""
        ).strip()
        try:
            guest_trial_fx_source_date = (
                date.fromisoformat(guest_trial_fx_source_date_text)
                if guest_trial_fx_source_date_text
                else None
            )
        except ValueError as exc:
            raise ValueError("DECAGENT_GUEST_TRIAL_FX_SOURCE_DATE must use YYYY-MM-DD") from exc
        guest_trial_fx_source_url = (
            os.getenv("DECAGENT_GUEST_TRIAL_FX_SOURCE_URL", "").strip() or None
        )
        fallback_key = os.getenv("DECAGENT_MAIN_FALLBACK_API_KEY", "")
        return cls(
            database_url=os.getenv("DECAGENT_DATABASE_URL", ""),
            data_dir=Path(os.getenv("DECAGENT_DATA_DIR", ".decagent")),
            public_url=os.getenv("DECAGENT_PUBLIC_URL", "http://127.0.0.1:8000"),
            main_model=os.getenv("DECAGENT_MAIN_MODEL", "gpt-4.1"),
            main_base_url=os.getenv("DECAGENT_MAIN_BASE_URL", "https://api.openai.com/v1"),
            main_api_key=os.getenv("DECAGENT_MAIN_API_KEY", ""),
            fallback_model=os.getenv(
                "DECAGENT_MAIN_FALLBACK_MODEL", "deepseek/deepseek-v4.1-flash"
            ),
            fallback_base_url=os.getenv(
                "DECAGENT_MAIN_FALLBACK_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            fallback_api_key=fallback_key,
            fallback_enabled=fallback_enabled == "true",
            guest_trial_enabled=guest_trial_enabled == "true",
            guest_trial_daily_cost_usd=guest_trial_daily_cost_usd,
            guest_trial_daily_qwen_tokens=guest_trial_daily_qwen_tokens,
            guest_trial_daily_tasks_per_guest=guest_trial_daily_tasks_per_guest,
            guest_trial_daily_qwen_tokens_per_guest=guest_trial_daily_qwen_tokens_per_guest,
            guest_trial_fx_source_date=guest_trial_fx_source_date,
            guest_trial_fx_cny_per_usd=guest_trial_fx_rate,
            guest_trial_fx_source_url=guest_trial_fx_source_url,
            guest_trial_fx_safety_margin_bps=guest_trial_fx_margin_bps,
            jev_model=os.getenv("DECAGENT_JEV_MODEL", "jev-1.13.0"),
            jev_provider=jev_provider,
            jev_api_key=jev_api_key,
            github_token=os.getenv("DECAGENT_GITHUB_TOKEN", ""),
        )

    @property
    def resolved_fallback_api_key(self):
        if self.fallback_api_key:
            return self.fallback_api_key
        return self.jev_api_key if self.jev_provider == "openrouter" else ""
