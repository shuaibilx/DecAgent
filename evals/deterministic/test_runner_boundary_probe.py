"""Unit tests for the runner's outbound-boundary readiness probe."""

from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path


def _probe_module():
    path = Path(__file__).resolve().parents[2] / "ops" / "runner" / "boundary_probe.py"
    spec = importlib.util.spec_from_file_location("decagent_runner_boundary_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FailingOpener:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def open(self, url, timeout):
        self.calls.append((url, timeout))
        raise self.error


def test_http_probe_counts_redirect_response_without_following_it():
    probe = _probe_module()
    opener = _FailingOpener(
        urllib.error.HTTPError("http://example.com/", 301, "Moved Permanently", {}, None)
    )

    assert probe.probe_succeeded(opener, "http://example.com/")
    assert opener.calls == [("http://example.com/", 15)]
    handler = probe.NoRedirectHandler()
    assert (
        handler.redirect_request(
            None, None, 301, "Moved", {}, "https://example.com/"
        )
        is None
    )


def test_http_probe_rejects_proxy_error_responses():
    probe = _probe_module()
    opener = _FailingOpener(
        urllib.error.HTTPError("http://example.com/", 403, "Forbidden", {}, None)
    )

    assert not probe.probe_succeeded(opener, "http://example.com/")
    assert len(opener.calls) == 1
