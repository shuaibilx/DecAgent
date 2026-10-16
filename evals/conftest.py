"""Offline tests must never contact external models by accident."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        if not request.config.getoption("--allow-live"):
            pytest.fail("Live tests require explicit --allow-live and a budget manifest")
        return
    original = socket.socket.connect

    def guarded(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == "localhost"
            if not allowed:
                raise RuntimeError(f"Offline test attempted external network: {host}")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def pytest_addoption(parser):
    parser.addoption("--allow-live", action="store_true", default=False)
