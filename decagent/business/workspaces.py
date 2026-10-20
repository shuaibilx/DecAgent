"""Workspace identity primitives shared by trusted authentication and storage paths."""

from uuid import UUID

LEGACY_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")


def parse_workspace_id(value):
    """Parse a server-trusted workspace UUID; request values must never be passed here."""
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("A trusted workspace identity is required") from exc
