"""Losslessly factor repeated JSON fragments in the trusted planner catalog.

This representation compresses instructions only. The compiler always validates
against the original registered contracts; no summaries replace their semantics.
"""

import json
from collections import Counter

from decagent.decisions.contracts import canonical_json


def pack_catalog(value):
    counts, fragments = Counter(), {}
    reserved = False

    def collect(item):
        nonlocal reserved
        if isinstance(item, dict):
            if set(item) == {"$catalog_ref"}:
                reserved = True
            key = canonical_json(item)
            if len(key) >= 160:
                counts[key] += 1
                fragments[key] = item
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    if reserved:
        return {"catalog": value, "shared_fragments": {}}
    names = {
        key: f"fragment_{index}"
        for index, key in enumerate(sorted(key for key, count in counts.items() if count > 1))
    }
    used = set()

    def compact(item, *, root=False):
        if isinstance(item, dict):
            key = canonical_json(item)
            if not root and key in names:
                used.add(names[key])
                return {"$catalog_ref": names[key]}
            return {key: compact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [compact(child) for child in item]
        return item

    catalog = compact(value)
    shared = {}
    # Add only transitively referenced fragments. Each reference points to a
    # strictly smaller source subtree, so no generated cycle can exist.
    while used - shared.keys():
        for key, name in names.items():
            if name in used and name not in shared:
                shared[name] = compact(fragments[key], root=True)
    return {"catalog": catalog, "shared_fragments": shared}


def expand_catalog(value):
    """Round-trip verifier for examples/tests; execution never trusts prompt refs."""
    definitions = value["shared_fragments"]

    def expand(item, stack=()):
        if isinstance(item, dict):
            if set(item) == {"$catalog_ref"} and item["$catalog_ref"] in definitions:
                name = item["$catalog_ref"]
                if name in stack:
                    raise ValueError("Cyclic prompt catalog reference")
                return expand(definitions[name], (*stack, name))
            return {key: expand(child, stack) for key, child in item.items()}
        if isinstance(item, list):
            return [expand(child, stack) for child in item]
        return item

    return json.loads(canonical_json(expand(value["catalog"])))
