import httpx
import pytest


def test_owner_client_reads_protected_file_and_never_puts_bearer_in_url(tmp_path):
    from decagent.gateway import OwnerClient

    token = "dca_" + "a" * 64
    path = tmp_path / "owner.token"
    path.write_text(token)
    requests = []

    def server(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + token
        assert token not in str(request.url)
        if request.method == "POST":
            assert request.headers["Idempotency-Key"]
        return httpx.Response(200, json={"state": "queued"})

    client = OwnerClient("http://127.0.0.1:8000", path, transport=httpx.MockTransport(server))
    assert client.task("00000000-0000-0000-0000-000000000001")["state"] == "queued"
    client.resume("00000000-0000-0000-0000-000000000001", budget={"max_calls": 40})
    client.cancel("00000000-0000-0000-0000-000000000001")
    client.close()
    assert len(requests) == 3


def test_owner_client_error_is_redacted_and_remote_http_is_refused(tmp_path):
    from decagent.gateway import OwnerClient

    path = tmp_path / "owner.token"
    path.write_text("dca_" + "a" * 64)
    with pytest.raises(ValueError, match="HTTPS"):
        OwnerClient("http://remote.example", path)
    client = OwnerClient(
        "http://127.0.0.1:8000",
        path,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409, json={"code": "stale_approval", "message": "sk-" + "b" * 30}
            )
        ),
    )
    with pytest.raises(RuntimeError, match="stale_approval") as error:
        client.resume("00000000-0000-0000-0000-000000000001")
    assert "b" * 30 not in str(error.value)
