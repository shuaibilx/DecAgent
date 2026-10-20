"""Closed, bounded vocabulary for model validation diagnostics.

Never carry exception messages, instance values, node IDs or arbitrary object keys.
Only trusted validators may translate their errors into this data contract.
"""

from dataclasses import dataclass

MAX_VALIDATION_ISSUES = 8
MAX_VALIDATION_PATH = 12
PLAN_FIELDS = frozenset(
    "answer cases collection command condition conditions content_digest depends_on description "
    "entry evidence goal id inputs kind left memory_kind mode next node_id nodes nullable "
    "on_error on_missing on_no_match op path plan_id plugin_id policy policy_id prompt provenance "
    "question question_id registry_fingerprint revision right schema_version source stop subject "
    "tool transform transform_id type value value_type version".split()
)
SCHEMA_CODES = frozenset(
    "schema_" + name for name in (
        "additionalProperties", "required", "type", "const", "enum", "oneOf", "anyOf",
        "allOf", "not", "pattern", "minLength", "maxLength", "minimum", "maximum",
        "minItems", "maxItems", "uniqueItems",
    )
)
COMPILE_CODES = frozenset(
    "duplicate_node entry dangling_edge dependency cycle unreachable branch_depth path_limit "
    "capability effect transform future_reference evaluation_receipt provenance branch_reference "
    "reference_type missing_parameter unknown_parameter authority input_type registry_changed "
    "evidence question_input candidate_source completion".split()
)
VALIDATION_CODES = SCHEMA_CODES | COMPILE_CODES | {"invalid_plan", "parent_revision"}


def safe_path(parts):
    """Retain only a bounded prefix made of fixed field names and bounded indices."""
    result = []
    for part in parts:
        if len(result) >= MAX_VALIDATION_PATH:
            break
        if not (
            (type(part) is str and part in PLAN_FIELDS)
            or (type(part) is int and 0 <= part <= 255)
        ):
            break
        result.append(part)
    return tuple(result)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: tuple = ()

    def __post_init__(self):
        if type(self.code) is not str or self.code not in VALIDATION_CODES:
            raise ValueError("Unsupported validation diagnostic code")
        if type(self.path) is not tuple or self.path != safe_path(self.path):
            raise ValueError("Unsupported validation diagnostic location")

    def to_mapping(self):
        return {"code": self.code, "path": list(self.path)}


def bounded_validation_issues(issues):
    if type(issues) not in {tuple, list}:
        return ()
    result = []
    for issue in issues[:MAX_VALIDATION_ISSUES]:
        if type(issue) is ValidationIssue:
            # Recheck at the provider boundary; do not trust arbitrary error attributes.
            try:
                result.append(ValidationIssue(issue.code, issue.path))
            except ValueError:
                continue
    return tuple(result)


class ModelValidationError(ValueError):
    """A trusted validator rejected data and supplied safe structural feedback."""

    def __init__(self, issues):
        self.validation_issues = bounded_validation_issues(issues)
        super().__init__("Model proposal failed structural validation")
