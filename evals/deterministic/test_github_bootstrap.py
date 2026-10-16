import httpx
import pytest

from decagent.integrations.bootstrap import create_private_test_repository
from decagent.integrations.github import GitHubError


def test_private_repository_setup_reconciles_lost_creation_response():
    calls, created = [], []
    name = "decagent-integration-test-fixture"
    repository = {
        "id": 2,
        "full_name": "owner/" + name,
        "private": True,
        "description": "Private sandbox for DecAgent integration verification.",
        "html_url": "https://github.com/owner/" + name,
        "default_branch": "main",
    }

    def handler(request):
        calls.append(request.method)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "owner", "id": 1})
        if request.method == "GET":
            return httpx.Response(200, json=repository) if created else httpx.Response(404)
        created.append(True)
        raise httpx.ReadTimeout("Lost response", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert create_private_test_repository("test-secret", name, client=client)["private"] is True
    assert create_private_test_repository("test-secret", name, client=client)["repository_id"] == 2
    assert calls.count("POST") == 1


def test_failed_credentials_never_expose_provider_body():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "secret-key"}))
    )
    with pytest.raises(GitHubError) as raised:
        create_private_test_repository(
            "secret-key", "decagent-integration-test-fixture", client=client
        )
    assert raised.value.status == 401 and "secret-key" not in str(raised.value)
