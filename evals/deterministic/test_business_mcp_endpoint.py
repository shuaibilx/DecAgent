import pytest

from decagent.business.mcp_client import _checked_endpoint


def test_mcp_client_accepts_only_the_pinned_compose_service_endpoint():
    assert _checked_endpoint("http://business-mcp:8001/mcp") == "http://business-mcp:8001/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "http://other-service:8001/mcp",
        "http://business-mcp:9999/mcp",
        "https://business-mcp:8001/mcp",
        "http://10.0.0.8:8001/mcp",
        "http://8.8.8.8:8001/mcp",
        "http://business-mcp:8001/other",
        "http://user@business-mcp:8001/mcp",
        "http://business-mcp:8001/mcp?redirect=https://example.com",
    ],
)
def test_mcp_client_rejects_noncanonical_internal_or_external_endpoints(url):
    with pytest.raises(ValueError):
        _checked_endpoint(url)


def test_mcp_client_preserves_loopback_endpoint_for_local_control():
    assert _checked_endpoint("http://127.0.0.1:8853/mcp") == "http://127.0.0.1:8853/mcp"
