import asyncio
import importlib

import pytest


def test_forwarded_client_ip_is_used_only_through_the_explicit_trusted_chain():
    module = importlib.import_module("decagent.business.guest_rate_limit")
    resolve = getattr(module, "resolve_client_ip", None)
    assert callable(resolve)

    proxies = ("192.0.2.0/24",)
    assert resolve("198.51.100.12", "203.0.113.99", proxies) == "198.51.100.12"
    assert resolve("192.0.2.2", "198.51.100.12", proxies) == "198.51.100.12"
    assert resolve("192.0.2.2", "198.51.100.12, 192.0.2.1", proxies) == "198.51.100.12"
    assert resolve("192.0.2.2", "bad-address, 192.0.2.1", proxies) == "192.0.2.2"
    assert resolve("192.0.2.2", "192.0.2.1, 192.0.2.3", proxies) == "192.0.2.2"


def test_trusted_proxy_configuration_parses_only_valid_cidrs(monkeypatch):
    from decagent.config import Settings

    assert getattr(Settings(), "guest_trusted_proxies", None) == ()
    monkeypatch.setenv("DECAGENT_GUEST_TRUSTED_PROXIES", "127.0.0.1/32, 10.20.0.0/16")
    assert Settings.from_env().guest_trusted_proxies == ("127.0.0.1/32", "10.20.0.0/16")
    monkeypatch.setenv("DECAGENT_GUEST_TRUSTED_PROXIES", "not-a-cidr")
    with pytest.raises(ValueError, match="trusted proxy"):
        Settings.from_env()


def test_enabled_guest_trial_requires_a_deployment_hmac_secret():
    from decagent.config import Settings

    with pytest.raises(ValueError, match="HMAC_KEY"):
        Settings(guest_trial_enabled=True)


def test_proxy_middleware_only_rewrites_scheme_for_an_explicitly_trusted_peer():
    module = importlib.import_module("decagent.business.guest_rate_limit")
    middleware_type = getattr(module, "TrustedProxyHeadersMiddleware", None)
    assert middleware_type is not None
    captured = {}

    async def capture(scope, receive, send):
        captured.update(scope)

    async def invoke(peer, headers):
        middleware = middleware_type(capture, trusted_proxies=("192.0.2.0/24",))
        scope = {
            "type": "http",
            "client": (peer, 443),
            "scheme": "http",
            "headers": headers,
            "state": {},
        }
        await middleware(scope, None, None)

    asyncio.run(
        invoke(
            "192.0.2.10",
            [(b"x-forwarded-for", b"198.51.100.5"), (b"x-forwarded-proto", b"https")],
        )
    )
    assert captured["scheme"] == "https"
    assert captured["state"]["guest_client_ip"] == "198.51.100.5"

    captured.clear()
    asyncio.run(
        invoke(
            "198.51.100.10",
            [(b"x-forwarded-for", b"203.0.113.5"), (b"x-forwarded-proto", b"https")],
        )
    )
    assert captured["scheme"] == "http"
    assert captured["state"]["guest_client_ip"] == "198.51.100.10"
