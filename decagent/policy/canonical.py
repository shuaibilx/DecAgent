"""Stable JSON hashing with explicit domains and no provider-dependent coercion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("Canonical object keys must be strings")
        return {key: primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("Unsupported canonical value")


def digest(domain: str, value: Any) -> str:
    encoded = json.dumps(
        {"domain": domain, "value": primitive(value)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
