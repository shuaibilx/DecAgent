"""Shared topology constants for the SOP Compose deployment."""

COMPOSE_INTERNAL_MCP_ENDPOINT = "http://business-mcp:8001/mcp"

SOP_REQUIRED_SERVICES = frozenset(
    {"postgres", "migrate", "bootstrap", "business-mcp", "api", "worker"}
)
SOP_PRIVATE_SERVICES = frozenset({"postgres", "migrate", "bootstrap", "business-mcp"})
