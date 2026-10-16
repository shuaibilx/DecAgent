"""Operative records must preserve their digest; sanitized audit summaries may be lossy."""

from decagent.policy import Redactor


def exact_safe_record(value, redactor):
    bounded = Redactor(
        known_secrets=redactor.known_secrets, max_text_chars=1_048_576, max_items=1000, max_depth=32
    )
    safe = bounded.sanitize(value, surface="evidence")
    if safe != value:
        raise ValueError(
            "Operative record contains suspected credentials or exceeds supported bounds"
        )
    return safe
