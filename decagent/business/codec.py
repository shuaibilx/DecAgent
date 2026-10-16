"""Transport-independent strict business JSON and result envelopes."""

import json
import math


def error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def strict_json(text):
    # Scan iteratively before json.loads can recurse. Quoted brackets are ordinary text.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON nesting depth exceeds 64")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value

    try:
        return json.loads(
            text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting depth exceeds parser bounds") from exc


def decode_json(raw: bytes, *, maximum: int = 65536):
    if len(raw) > maximum:
        raise ValueError("Business JSON exceeds byte limit")
    return strict_json(raw.decode("utf-8"))


def validate_envelope(response, schema, *, maximum=262144):
    from decagent.business.contracts import canonical, validate_schema

    try:
        encoded = canonical(response).encode("utf-8")
    except RecursionError as exc:
        raise ValueError("JSON nesting depth exceeds parser bounds") from exc
    if len(encoded) > maximum:
        raise ValueError("Business result exceeds byte limit")
    strict_json(encoded.decode("utf-8"))
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise ValueError("Expected strict JSON result envelope")
    if response["ok"]:
        if set(response) != {"ok", "data"}:
            raise ValueError("Invalid success envelope")
        validate_schema(schema, response["data"])
    elif (
        set(response) != {"ok", "error"}
        or not isinstance(response["error"], dict)
        or set(response["error"]) != {"code", "message"}
        or not all(isinstance(response["error"][k], str) and response["error"][k]
                   for k in ("code", "message"))
    ):
        raise ValueError("Invalid error envelope")
    return response
