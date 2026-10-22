"""Shared topology constants for the SOP Compose deployment."""

SOP_REQUIRED_SERVICES = frozenset(
    {"postgres", "migrate", "bootstrap", "business-mcp", "api", "worker"}
)
SOP_PRIVATE_SERVICES = frozenset({"postgres", "migrate", "bootstrap", "business-mcp"})
