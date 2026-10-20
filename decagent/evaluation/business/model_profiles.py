"""Secret-free, content-addressed model identities for controlled evaluations."""

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from decagent.decisions.contracts import digest

from .comparison import UNBOUND_MODEL_PROFILE_DIGEST

_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PROVIDER = _ROLE
_MODEL = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/~-]{0,255}\Z")
_PRICE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}\Z")


@dataclass(frozen=True)
class ModelProfile:
    """Frozen, non-secret identity of one model role and its execution policy."""

    role: str
    provider: str
    model: str
    endpoint: str
    price_version: str
    price_source: str
    input_price_per_million: Decimal | None
    output_price_per_million: Decimal | None
    max_input_tokens: int
    max_output_tokens: int
    max_question_tokens: int | None = None
    fallback_allowed: bool = False
    max_provider_attempts: int = 1
    fallback_profile_digest: str | None = None
    temperature: Decimal | None = Decimal("0")
    top_p: Decimal | None = Decimal("1")
    seed: int | None = None
    unsupported_parameters: tuple[str, ...] = ("seed",)
    response_mode: str = "chat_tool_or_text"
    tool_choice: str = "auto"
    timeout_seconds: int = 60
    provider_routing: str = "direct_endpoint"
    pricing_status: str = "known"

    def __post_init__(self):
        if type(self.role) is not str or not _ROLE.fullmatch(self.role):
            raise ValueError("Model role must be a stable lowercase identifier")
        if type(self.provider) is not str or not _PROVIDER.fullmatch(self.provider):
            raise ValueError("Model provider must be a stable lowercase identifier")
        if type(self.model) is not str or not _MODEL.fullmatch(self.model):
            raise ValueError("Model identifier is invalid")
        if type(self.price_version) is not str or not _PRICE_VERSION.fullmatch(
            self.price_version
        ):
            raise ValueError("A versioned model price policy is required")
        if (
            type(self.price_source) is not str
            or not self.price_source.strip()
            or len(self.price_source) > 512
        ):
            raise ValueError("Model price provenance is required")
        if type(self.endpoint) is not str:
            raise ValueError("Model endpoint must be an HTTPS URL")
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Model endpoint must be an HTTPS URL without credentials or query")
        if self.pricing_status == "unknown":
            if (
                self.input_price_per_million is not None
                or self.output_price_per_million is not None
            ):
                raise ValueError("Unknown pricing requires both rates to be null")
        elif self.pricing_status == "known":
            if any(
                type(rate) is not Decimal or not rate.is_finite() or rate < 0
                for rate in (self.input_price_per_million, self.output_price_per_million)
            ):
                raise ValueError("Known pricing requires finite nonnegative Decimal rates")
        else:
            raise ValueError("Pricing status must be known or unknown")
        if type(self.max_input_tokens) is not int or not 1 <= self.max_input_tokens <= 65_536:
            raise ValueError("Model input limit must be between one and 65,536")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 8_192:
            raise ValueError("Model output limit must be between one and 8,192")
        if self.role == "jev":
            if (
                type(self.max_question_tokens) is not int
                or not 1 <= self.max_question_tokens <= 32_768
            ):
                raise ValueError("Jev profile requires a per-question context limit")
        elif self.max_question_tokens is not None:
            raise ValueError("Per-question context limits only apply to Jev profiles")
        if type(self.fallback_allowed) is not bool:
            raise ValueError("Fallback policy must be boolean")
        if self.fallback_allowed:
            if type(self.fallback_profile_digest) is not str or not re.fullmatch(
                r"[a-f0-9]{64}\Z", self.fallback_profile_digest
            ):
                raise ValueError("Enabled fallback requires a separately frozen profile")
        elif self.fallback_profile_digest is not None:
            raise ValueError("Disabled fallback cannot carry a fallback profile")
        parameter_names = {"temperature", "top_p", "seed"}
        if (
            type(self.unsupported_parameters) is not tuple
            or any(type(name) is not str for name in self.unsupported_parameters)
            or len(set(self.unsupported_parameters)) != len(self.unsupported_parameters)
            or not set(self.unsupported_parameters) <= parameter_names
        ):
            raise ValueError("Unsupported parameter declarations are invalid")
        for name, value, low, high in (
            ("temperature", self.temperature, Decimal("0"), Decimal("2")),
            ("top_p", self.top_p, Decimal("0"), Decimal("1")),
        ):
            if name in self.unsupported_parameters:
                if value is not None:
                    raise ValueError(f"Declared unsupported parameter {name} must not be sent")
            elif (
                type(value) is not Decimal or not value.is_finite() or not low <= value <= high
            ):
                raise ValueError(f"Supported parameter {name} requires a fixed bounded value")
        if "seed" in self.unsupported_parameters:
            if self.seed is not None:
                raise ValueError("Declared unsupported parameter seed must not be sent")
        elif type(self.seed) is not int or not 0 <= self.seed <= 2_147_483_647:
            raise ValueError("Supported seed requires a fixed nonnegative integer")
        if type(self.response_mode) is not str or self.response_mode not in {
            "chat_tool_or_text",
            "chat_text",
            "jev_decisions",
        }:
            raise ValueError("Model response mode is invalid")
        if type(self.tool_choice) is not str or self.tool_choice not in {
            "auto",
            "none",
            "not_applicable",
        }:
            raise ValueError("Model tool-choice policy is invalid")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Model timeout must be between one and 120 seconds")
        if type(self.provider_routing) is not str or self.provider_routing not in {
            "direct_endpoint", "openrouter_default", "openrouter_model_alias"
        }:
            raise ValueError("Provider-routing policy is invalid")
        if self.role == "jev":
            if (
                self.response_mode != "jev_decisions"
                or self.tool_choice != "not_applicable"
                or self.provider_routing != "openrouter_model_alias"
                or self.timeout_seconds > 10
                or not {"temperature", "top_p", "seed"} <= set(self.unsupported_parameters)
            ):
                raise ValueError("Jev profile must declare its Decisions API request contract")
        elif (
            self.response_mode == "jev_decisions"
            or self.provider_routing == "openrouter_model_alias"
        ):
            raise ValueError("General-model profile cannot use the Jev Decisions routing contract")
        elif self.response_mode == "chat_text" and self.tool_choice != "none":
            raise ValueError("Text-only profile must disable tool selection")
        elif (
            self.response_mode == "chat_tool_or_text"
            and self.tool_choice == "not_applicable"
        ):
            raise ValueError("Chat profile must define a tool-choice policy")
        attempt_limit = 3 if self.role == "jev" else 2
        if (
            type(self.max_provider_attempts) is not int
            or not 1 <= self.max_provider_attempts <= attempt_limit
        ):
            raise ValueError(
                "Same-provider attempts exceed the role-specific retry bound"
            )

    @property
    def fingerprint(self):
        """Digest of provider, model, endpoint, prices and safety limits; no key material."""
        return digest(self.to_dict())

    def to_dict(self):
        values = asdict(self)
        if self.pricing_status == "unknown":
            values["input_price_per_million"] = None
            values["output_price_per_million"] = None
        else:
            values.pop("pricing_status")
            values["input_price_per_million"] = str(self.input_price_per_million)
            values["output_price_per_million"] = str(self.output_price_per_million)
        values["temperature"] = str(self.temperature) if self.temperature is not None else None
        values["top_p"] = str(self.top_p) if self.top_p is not None else None
        values["unsupported_parameters"] = list(self.unsupported_parameters)
        return values


@dataclass(frozen=True)
class EvaluationModelProfiles:
    """General-model roles frozen for one public evaluation cohort."""

    primary: ModelProfile
    user_simulator: ModelProfile
    judge: ModelProfile
    jev: ModelProfile | None = None

    def __post_init__(self):
        for role in ("primary", "user_simulator", "judge"):
            profile = getattr(self, role)
            if not isinstance(profile, ModelProfile) or profile.role != role:
                raise ValueError("Evaluation profile does not match role " + role)
        if self.jev is not None and (
            not isinstance(self.jev, ModelProfile) or self.jev.role != "jev"
        ):
            raise ValueError("Evaluation Jev profile does not match role jev")

    @property
    def digests(self):
        return {
            "model_profile_digest": self.primary.fingerprint,
            "jev_profile_digest": (
                self.jev.fingerprint if self.jev else UNBOUND_MODEL_PROFILE_DIGEST
            ),
            "user_simulator_profile_digest": self.user_simulator.fingerprint,
            "judge_profile_digest": self.judge.fingerprint,
        }

    def to_dict(self):
        role_names = ("primary", "user_simulator", "judge") + (("jev",) if self.jev else ())
        profiles = {
            role: {**getattr(self, role).to_dict(), "fingerprint": getattr(self, role).fingerprint}
            for role in role_names
        }
        version = (
            "evaluation-model-profiles-v3"
            if any(getattr(self, role).pricing_status == "unknown" for role in role_names)
            else "evaluation-model-profiles-v2"
        )
        payload = {"schema_version": version, "roles": profiles}
        return {**payload, "fingerprint": digest(payload)}

    @property
    def fingerprint(self):
        document = self.to_dict()
        return document["fingerprint"]

    @classmethod
    def from_dict(cls, value):
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "roles", "fingerprint"}
            or value["schema_version"] not in {
                "evaluation-model-profiles-v2", "evaluation-model-profiles-v3"
            }
            or type(value["roles"]) is not dict
            or not {"primary", "user_simulator", "judge"} <= set(value["roles"])
            or not set(value["roles"]) <= {"primary", "user_simulator", "judge", "jev"}
        ):
            raise ValueError("Invalid evaluation model profile document")
        payload = {key: value[key] for key in ("schema_version", "roles")}
        if type(value["fingerprint"]) is not str or value["fingerprint"] != digest(payload):
            raise ValueError("Evaluation model profile document fingerprint mismatch")
        profiles = {}
        version = value["schema_version"]
        unknown_pricing_seen = False
        expected_fields = {
            "role",
            "provider",
            "model",
            "endpoint",
            "price_version",
            "price_source",
            "input_price_per_million",
            "output_price_per_million",
            "max_input_tokens",
            "max_output_tokens",
            "max_question_tokens",
            "fallback_allowed",
            "max_provider_attempts",
            "fallback_profile_digest",
            "temperature",
            "top_p",
            "seed",
            "unsupported_parameters",
            "response_mode",
            "tool_choice",
            "timeout_seconds",
            "provider_routing",
            "fingerprint",
        }
        for role, record in value["roles"].items():
            record_fields = set(record) if type(record) is dict else set()
            pricing_status = record.get("pricing_status", "known") if type(record) is dict else None
            if (
                type(record) is not dict
                or frozenset(record_fields) not in {
                    frozenset(expected_fields),
                    frozenset(expected_fields | {"pricing_status"}),
                }
                or (version == "evaluation-model-profiles-v2" and "pricing_status" in record_fields)
                or pricing_status not in {"known", "unknown"}
            ):
                raise ValueError("Invalid frozen model profile fields")
            unknown_pricing_seen = unknown_pricing_seen or pricing_status == "unknown"
            profile_values = {key: item for key, item in record.items() if key != "fingerprint"}
            if pricing_status == "unknown":
                if (
                    profile_values["input_price_per_million"] is not None
                    or profile_values["output_price_per_million"] is not None
                ):
                    raise ValueError("Unknown pricing requires both rates to be null")
                profile_values["pricing_status"] = "unknown"
            else:
                profile_values["input_price_per_million"] = Decimal(
                    profile_values["input_price_per_million"]
                )
                profile_values["output_price_per_million"] = Decimal(
                    profile_values["output_price_per_million"]
                )
            if profile_values["temperature"] is not None:
                profile_values["temperature"] = Decimal(profile_values["temperature"])
            if profile_values["top_p"] is not None:
                profile_values["top_p"] = Decimal(profile_values["top_p"])
            if type(profile_values["unsupported_parameters"]) is not list:
                raise ValueError("Unsupported parameter declarations must be an array")
            profile_values["unsupported_parameters"] = tuple(
                profile_values["unsupported_parameters"]
            )
            profile = ModelProfile(**profile_values)
            if record["fingerprint"] != profile.fingerprint:
                raise ValueError("Frozen model profile fingerprint mismatch")
            profiles[role] = profile
        if version == "evaluation-model-profiles-v3" and not unknown_pricing_seen:
            raise ValueError("Version 3 profiles require at least one unknown pricing status")
        return cls(**profiles)

