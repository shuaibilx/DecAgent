"""Strict, bounded JSON at the business boundary, independent of its transport."""

from importlib.util import find_spec

import pytest


def test_codec_is_independent_of_subprocess_transport():
    assert find_spec("decagent.business.codec") is not None


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":1e999}'])
def test_noncanonical_json_is_rejected(text):
    from decagent.business.codec import strict_json

    with pytest.raises(ValueError):
        strict_json(text)


def test_json_bounds_count_utf8_bytes():
    from decagent.business.codec import decode_json

    assert decode_json(b'{"x":"a"}', maximum=9) == {"x": "a"}
    with pytest.raises(ValueError, match="limit"):
        decode_json('{"x":"中"}'.encode(), maximum=9)


def test_strict_json_bounds_depth_but_ignores_brackets_in_strings():
    from decagent.business.codec import strict_json

    with pytest.raises(ValueError, match="depth|nesting"):
        strict_json("[" * 3000 + "0" + "]" * 3000)
    with pytest.raises(ValueError, match="finite"):
        strict_json("1e999")
    assert strict_json('"' + "[" * 3000 + '"') == "[" * 3000


def test_structured_result_enforces_the_same_json_depth_limit():
    from decagent.business.codec import validate_envelope

    response = {"ok": True, "data": {"nested": []}}
    nested = response["data"]["nested"]
    for _ in range(65):
        child = []
        nested.append(child)
        nested = child
    with pytest.raises(ValueError, match="nesting depth"):
        validate_envelope(response, {"type": "object"})


@pytest.mark.parametrize(
    "response",
    [
        {"ok": 1, "data": {}},
        {"ok": True, "data": {}, "role": "supervisor"},
        {"ok": False, "error": {"code": "x", "message": "x", "extra": True}},
        {"ok": False, "error": {"code": "", "message": "x"}},
    ],
)
def test_strict_result_envelopes(response):
    from decagent.business.codec import validate_envelope

    with pytest.raises(ValueError):
        validate_envelope(response, {"type": "object"})


def test_result_schema_and_valid_error():
    from decagent.business.codec import error, validate_envelope

    schema = {"type": "object", "required": ["id"]}
    with pytest.raises(ValueError):
        validate_envelope({"ok": True, "data": {}}, schema)
    result = error("not_accessible", "Resource unavailable")
    assert validate_envelope(result, schema) == result
