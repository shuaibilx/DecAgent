"""Every egress surface uses the same bounded secret filter."""

import json

import pytest

from decagent.policy import Redactor


@pytest.mark.parametrize("surface", ["log", "evidence", "ui", "model"])
def test_known_secrets_are_removed_from_all_surfaces(surface):
    secret = "test-private-secret-with-no-recognizable-prefix"
    redactor = Redactor(known_secrets=(secret,))
    sanitized = redactor.sanitize(
        {"message": f"error: {secret}", "nested": [secret]}, surface=surface
    )
    assert secret not in json.dumps(sanitized)
    assert "[REDACTED]" in json.dumps(sanitized)


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "github_pat_" + "a" * 40,
        "sk-" + "a" * 40,
        "AKIA" + "A" * 16,
        "Bearer abcdefghijk987654321",
        "password=veryprivatevalue",
        "-----BEGIN PRIVATE KEY-----\nprivate-bytes\n-----END PRIVATE KEY-----",
        "https://user:privatepass@example.com/repo",
    ],
)
def test_known_credential_patterns_are_redacted(secret):
    assert secret not in Redactor().redact_text(f"error {secret} done")


def test_sensitive_keys_are_redacted_and_lengths_bounded():
    redactor = Redactor(max_text_chars=80, max_items=3)
    result = redactor.sanitize(
        {"api_key": "tiny", "safe": "a" * 1000, "items": list(range(20))}, surface="log"
    )
    assert result["api_key"] == "[REDACTED]"
    assert len(result["safe"]) <= 80
    assert len(result["items"]) <= 3


def test_model_payload_has_explicit_fields_and_safe_scoped_snippets():
    secret = "special-exact-secret"
    redactor = Redactor(known_secrets=(secret,), max_text_chars=100)
    result = redactor.minimize_for_model(
        {
            "task_id": "task-1",
            "goal": "Fix parser",
            "unrequested": secret,
            "history": "everything",
            "snippets": [{"path": "src/a.py", "text": secret}, {"path": ".env", "text": "private"}],
        },
        fields=("task_id", "goal", "snippets"),
    )
    assert set(result) == {"task_id", "goal", "snippets"}
    assert len(result["snippets"]) == 1
    assert secret not in json.dumps(result)


def test_redaction_preserves_empty_known_secret_and_small_safe_data():
    redactor = Redactor(known_secrets=("", "secret"))
    assert redactor.redact_text("ordinary") == "ordinary"
    assert redactor.sanitize({"count": 2, "ok": True}, surface="ui") == {"count": 2, "ok": True}


def test_invalid_surface_and_unknown_model_fields_are_rejected():
    with pytest.raises(ValueError):
        Redactor().sanitize("text", surface="unbounded")
    with pytest.raises(ValueError):
        Redactor().minimize_for_model({"entire_database": "x"}, fields=("entire_database",))


@pytest.mark.parametrize(
    "secret",
    [
        'password="a secret with spaces"',
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "AWS_SECRET_ACCESS_KEY=abcdef987654321",
        "xoxb-123456789012-abcdefghijk",
    ],
)
def test_quoted_and_http_credentials_fully_redacted(secret):
    result = Redactor().redact_text(secret)
    assert result == "[REDACTED]"


@pytest.mark.parametrize(
    "text",
    [
        "diagnostic token=SECRET",
        'token="a quoted secret value"',
        "access_token='value containing spaces'",
    ],
)
def test_bare_and_quoted_token_assignments_fully_redacted(text):
    result = Redactor(known_secrets=("SECRET",)).redact_text(text)
    assert "SECRET" not in result
    assert "quoted secret" not in result
    assert "containing spaces" not in result
    assert result in {"diagnostic [REDACTED]", "[REDACTED]"}


def test_known_secret_spanning_chunks_is_redacted_after_assembly():
    chunks = ["error token=", "very-", "private-", "secret"]
    redactor = Redactor(known_secrets=("very-private-secret",))
    assert redactor.redact_text("".join(chunks)) == "error [REDACTED]"
