"""One bounded filter shared by persistence, evidence, SSE/UI and model egress.

Known secret filtering and common credential patterns are defense in depth, not
proof that arbitrary content contains no secrets. Sensitive files never enter this
pipeline. The service must fail closed when additional suspect content is detected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .paths import normalize_relative_path

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
SURFACES = frozenset({"log", "evidence", "ui", "model"})
SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|"
    r"private[_-]?key|access[_-]?key|client[_-]?secret)(?:$|[_-])",
    re.I,
)
CREDENTIAL_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|\Z)",
        re.S,
    ),
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_.~+/=-]{8,}", re.I),
    re.compile(r"\bAuthorization\s*:\s*Basic\s+[A-Za-z0-9+/=]{8,}", re.I),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|(?:access[_-]?)?token|secret|password|passwd|client[_-]?secret|"
        r"(?:aws[_-]?)?secret[_-]?access[_-]?key)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"',;]+)",
        re.I,
    ),
    re.compile(r"https?://[^\s/:@]+:[^\s/@]+@[^\s]+", re.I),
)
MODEL_FIELDS = frozenset(
    {
        "task_id",
        "goal",
        "authorization_summary",
        "action",
        "risk_features",
        "code_version",
        "evidence_version",
        "snippets",
        "failure_summary",
        "decisions",
        "candidates",
        "query",
        "current_version",
        "memory_summary",
        "blocked_reason",
        "question",
        "evidence_summary",
    }
)


class Redactor:
    def __init__(
        self,
        *,
        known_secrets: Sequence[str] = (),
        max_text_chars: int = 65536,
        max_items: int = 100,
        max_depth: int = 8,
    ) -> None:
        if type(max_text_chars) is not int or not 32 <= max_text_chars <= 1_048_576:
            raise ValueError("Text limit must be between 32 and 1048576")
        if type(max_items) is not int or not 1 <= max_items <= 1000:
            raise ValueError("Collection limit must be between 1 and 1000")
        if type(max_depth) is not int or not 1 <= max_depth <= 32:
            raise ValueError("Depth limit must be between 1 and 32")
        if type(known_secrets) not in (list, tuple) or any(
            type(secret) is not str for secret in known_secrets
        ):
            raise ValueError("Known secrets must be a string sequence")
        self.known_secrets = tuple(sorted(set(filter(None, known_secrets)), key=len, reverse=True))
        self.max_text_chars = max_text_chars
        self.max_items = max_items
        self.max_depth = max_depth

    def redact_text(self, value: str) -> str:
        if type(value) is not str:
            raise ValueError("Text must be a string")
        # Redact before truncation so cutting through a credential cannot expose it.
        for secret in self.known_secrets:
            value = value.replace(secret, REDACTED)
        for pattern in CREDENTIAL_PATTERNS:
            value = pattern.sub(REDACTED, value)
        if len(value) > self.max_text_chars:
            value = value[: self.max_text_chars - len(TRUNCATED)] + TRUNCATED
        return value

    def sanitize(self, value: Any, *, surface: str) -> Any:
        if surface not in SURFACES:
            raise ValueError("Unknown output surface")
        return self._sanitize(value, 0)

    def _sanitize(self, value: Any, depth: int) -> Any:
        if depth >= self.max_depth:
            return TRUNCATED
        if type(value) is str:
            return self.redact_text(value)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= self.max_items:
                    break
                if type(key) is not str:
                    raise ValueError("Output objects require string keys")
                safe_key = self.redact_text(key)
                result[safe_key] = (
                    REDACTED if SECRET_KEY.search(key) else self._sanitize(item, depth + 1)
                )
            return result
        if type(value) in (list, tuple):
            return [self._sanitize(item, depth + 1) for item in value[: self.max_items]]
        if value is None or type(value) in (int, bool):
            return value
        if type(value) is float:
            import math

            return value if math.isfinite(value) else REDACTED
        raise ValueError("Unsupported output type; convert to a safe DTO first")

    def minimize_for_model(
        self,
        value: Mapping[str, Any],
        *,
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Select task-required fields before filtering; omit secret file snippets."""
        if (
            not isinstance(value, Mapping)
            or type(fields) not in (list, tuple)
            or any(type(field) is not str or field not in MODEL_FIELDS for field in fields)
        ):
            raise ValueError("Model fields must be an explicit approved field selection")
        selected = {key: value[key] for key in fields if key in value}
        if "snippets" in selected:
            snippets = selected["snippets"]
            if type(snippets) not in (list, tuple):
                raise ValueError("Model snippets must be an array")
            safe = []
            for snippet in snippets[: self.max_items]:
                if not isinstance(snippet, Mapping) or type(snippet.get("text")) is not str:
                    raise ValueError("Model snippets require path and text")
                try:
                    path = normalize_relative_path(snippet.get("path"))
                except ValueError:
                    continue
                safe.append({"path": path, "text": snippet["text"]})
            selected["snippets"] = safe
        return self.sanitize(selected, surface="model")
