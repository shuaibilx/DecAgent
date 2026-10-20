"""Inspect closed local JSON schemas without evaluating expressions or remote refs."""

JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})


def _literal_type(value):
    if value is None:
        return "null"
    return {
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        dict: "object",
        list: "array",
    }[type(value)]


def schema_variants(root, schema, seen=frozenset()):
    """Return structural variants, resolving only the root's local references."""
    if not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            raise ValueError("Schema reference must be finite and local")
        resolved = root
        for part in ref[2:].split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        return schema_variants(root, resolved, seen | {ref})
    variants = [dict(schema)]
    for choice in ("oneOf", "anyOf"):
        if choice in schema:
            variants = []
            base = {key: value for key, value in schema.items() if key != choice}
            for branch in schema[choice]:
                for item in schema_variants(root, branch, seen):
                    variants.append({**base, **item})
    if "allOf" in schema:
        base = {key: value for key, value in schema.items() if key != "allOf"}
        for branch in schema["allOf"]:
            for item in schema_variants(root, branch, seen):
                base = {
                    **base,
                    **item,
                    "properties": {**base.get("properties", {}), **item.get("properties", {})},
                    "required": list(set(base.get("required", [])) | set(item.get("required", []))),
                }
        variants = [base]
    return variants


def schema_types(root, schema):
    types = set()
    for variant in schema_variants(root, schema):
        kind = variant.get("type")
        if isinstance(kind, str):
            types.add(kind)
        elif isinstance(kind, list):
            types.update(kind)
        elif "const" in variant:
            types.add(_literal_type(variant["const"]))
        elif "enum" in variant:
            types.update(_literal_type(value) for value in variant["enum"])
        elif "properties" in variant:
            types.add("object")
        else:
            types.update(JSON_TYPES)
    return frozenset(types)


def schema_at_path(root, schema, path, *, allow_open_evaluation_result=False):
    current = [schema]
    for part in path:
        found = []
        for parent in current:
            for variant in schema_variants(root, parent):
                if (
                    allow_open_evaluation_result
                    and variant.get("x-decagent-open-result-path") is True
                ):
                    found.append(
                        {
                            "type": sorted(JSON_TYPES),
                            "x-decagent-open-result-path": True,
                        }
                    )
                    continue
                if isinstance(part, str):
                    properties = variant.get("properties", {})
                    if part in properties:
                        found.append(properties[part])
                    elif isinstance(variant.get("additionalProperties"), dict):
                        found.append(variant["additionalProperties"])
                elif type(part) is int:
                    items = variant.get("items")
                    prefix = variant.get("prefixItems", [])
                    if part < len(prefix):
                        found.append(prefix[part])
                    elif isinstance(items, dict):
                        found.append(items)
        if not found:
            raise ValueError("Unknown result or input path: " + ".".join(map(str, path)))
        current = found
    return current[0] if len(current) == 1 else {"anyOf": current}


def type_compatible(declared, allowed):
    return declared in allowed or (declared == "integer" and "number" in allowed)
