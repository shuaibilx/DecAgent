"""Reviewable experiment manifests; no credentials or execution authority are included."""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from urllib.parse import urlsplit

from decagent.decisions.contracts import digest


def amount(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Invalid monetary amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid monetary amount") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Monetary amounts must be finite and nonnegative")
    return result


def money(value) -> str:
    return format(amount(value).normalize(), "f")


def positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA256 digest")


@dataclass(frozen=True)
class ModelBinding:
    requested: str
    actual: str

    def __post_init__(self):
        for value in (self.requested, self.actual):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("Models require nonempty bounded identifiers")
        if self.actual.startswith("~") or self.actual.endswith(("-latest", "-preview")):
            raise ValueError("Actual model must be resolved, not an alias")


@dataclass(frozen=True)
class PriceBasis:
    version: str
    source_url: str
    checked_at: str
    input_per_million_usd: str
    output_per_million_usd: str

    def __post_init__(self):
        url = urlsplit(self.source_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or not self.version
            or not self.checked_at
        ):
            raise ValueError("Prices require a dated, credential-free HTTPS source")
        for field in ("input_per_million_usd", "output_per_million_usd"):
            object.__setattr__(self, field, money(getattr(self, field)))

    def reserve(self, inputs: int, outputs: int) -> Decimal:
        return (
            inputs * amount(self.input_per_million_usd)
            + outputs * amount(self.output_per_million_usd)
        ) / 1_000_000


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    dataset_version: str
    dataset_sha256: str
    sample_ids: tuple[str, ...]
    strategies: tuple[str, ...]
    primary_model: ModelBinding
    judges: Mapping[str, ModelBinding]
    authorization_sha256: str
    tools_sha256: str
    hard_policy_sha256: str
    acceptance_sha256: str
    max_calls: int
    max_tokens: int
    max_usd: str
    paid: bool = False
    price: PriceBasis | None = None
    format_version: str = "1"
    model_prices: Mapping[str, PriceBasis] | None = None

    def __post_init__(self):
        if self.format_version != "1" or not self.dataset_version:
            raise ValueError("Unsupported or unversioned run manifest")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.run_id):
            raise ValueError("Invalid run identifier")
        for name in (
            "dataset_sha256",
            "authorization_sha256",
            "tools_sha256",
            "hard_policy_sha256",
            "acceptance_sha256",
        ):
            sha256(getattr(self, name))
        for values in (self.sample_ids, self.strategies):
            if not values or len(set(values)) != len(values):
                raise ValueError("Samples and strategies must be nonempty and unique")
        if any(not isinstance(s, str) or "/" in s or not s for s in self.sample_ids):
            raise ValueError("Invalid sample identifier")
        if set(self.strategies) - {"rule", "main-llm", "jev"}:
            raise ValueError("Unknown decision strategy")
        if set(self.judges) != set(self.strategies):
            raise ValueError("Every strategy must bind its judge")
        if any(not isinstance(v, ModelBinding) for v in self.judges.values()):
            raise ValueError("Invalid model bindings")
        if not isinstance(self.primary_model, ModelBinding) or type(self.paid) is not bool:
            raise ValueError("Invalid primary model or paid flag")
        positive_int(self.max_calls, "Call cap")
        positive_int(self.max_tokens, "Token cap")
        object.__setattr__(self, "max_usd", money(self.max_usd))
        if self.model_prices is not None:
            models = {v.actual for v in self.judges.values()} | {self.primary_model.actual}
            if models - set(self.model_prices) or any(
                not isinstance(v, PriceBasis) for v in self.model_prices.values()
            ):
                raise ValueError("Price table must cover every bound actual model")
            object.__setattr__(self, "model_prices", MappingProxyType(dict(self.model_prices)))
        if self.paid and (
            amount(self.max_usd) == 0
            or (not isinstance(self.price, PriceBasis) and self.model_prices is None)
        ):
            raise ValueError("Paid benchmarks require prices and a positive fee cap")
        object.__setattr__(self, "sample_ids", tuple(self.sample_ids))
        object.__setattr__(self, "strategies", tuple(self.strategies))
        object.__setattr__(self, "judges", MappingProxyType(dict(self.judges)))

    def to_dict(self):
        return {
            **{
                k: v
                for k, v in vars(self).items()
                if k not in {"judges", "primary_model", "price", "model_prices"}
            },
            "judges": {k: asdict(v) for k, v in self.judges.items()},
            "primary_model": asdict(self.primary_model),
            "price": asdict(self.price) if self.price else None,
            "model_prices": {k: asdict(v) for k, v in self.model_prices.items()}
            if self.model_prices is not None
            else None,
        }

    @property
    def sha256(self):
        return digest(self.to_dict())

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["primary_model"] = ModelBinding(**values["primary_model"])
        values["judges"] = {k: ModelBinding(**v) for k, v in values["judges"].items()}
        values["price"] = PriceBasis(**values["price"]) if values["price"] else None
        values["model_prices"] = (
            {k: PriceBasis(**v) for k, v in values["model_prices"].items()}
            if values.get("model_prices") is not None
            else None
        )
        return cls(**values)


@dataclass(frozen=True)
class RunAuthorization:
    manifest_sha256: str
    confirmed_by: str
    confirmation_reference: str

    def __post_init__(self):
        sha256(self.manifest_sha256)
        if not self.confirmed_by or not self.confirmation_reference:
            raise ValueError("Run authorization needs an owner and confirmation reference")
