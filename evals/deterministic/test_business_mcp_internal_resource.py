import pytest

from decagent.business.mcp_server import create_mcp_app


class _Database:
    url = "postgresql://decagent:secret@postgres/decagent"


class _EmployeeAuth:
    pass


def test_internal_container_bind_requires_a_canonical_mcp_resource_url():
    app = create_mcp_app(
        _Database(),
        _EmployeeAuth(),
        host="0.0.0.0",
        port=8001,
        resource_url="http://business-mcp:8001/mcp",
    )

    assert app.state.business_mcp.resource_url == "http://business-mcp:8001/mcp"


def test_non_loopback_mcp_bind_without_internal_identity_is_rejected():
    with pytest.raises(ValueError, match="internal resource URL"):
        create_mcp_app(_Database(), _EmployeeAuth(), host="0.0.0.0", port=8001)


def test_internal_mcp_resource_must_match_service_port_and_path():
    with pytest.raises(ValueError, match="resource URL"):
        create_mcp_app(
            _Database(),
            _EmployeeAuth(),
            host="0.0.0.0",
            port=8001,
            resource_url="http://business-mcp:9999/not-mcp",
        )
