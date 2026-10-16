"""Platform selection and fair serial dispatch do not initialize repair capabilities."""

import sys
from types import SimpleNamespace

import pytest

from decagent.runtime import worker as runtime_worker


@pytest.mark.parametrize(
    ("platform", "kind", "expected"),
    [
        ("win32", None, ["business", "repository_repair"]),
        ("win32", "business", ["business"]),
        ("win32", "repository_repair", ["repository_repair"]),
        ("linux", None, ["business", "repository_repair"]),
        ("linux", "business", ["business"]),
        ("linux", "repository_repair", ["repository_repair"]),
    ],
)
def test_platform_dispatch_selects_only_supported_task_workers(
    monkeypatch, platform, kind, expected
):
    import decagent.business.worker
    import decagent.storage.database

    selected = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(decagent.storage.database, "Database", lambda url: "database")
    monkeypatch.setattr(
        runtime_worker, "Worker", lambda *args: SimpleNamespace(kind="repository_repair")
    )
    monkeypatch.setattr(
        runtime_worker,
        "_run_serial_workers",
        lambda workers: selected.extend(w.kind for w in workers),
        raising=False,
    )
    monkeypatch.setattr(
        decagent.business.worker, "BusinessWorker", lambda *args: SimpleNamespace(kind="business")
    )
    runtime_worker.run_worker(SimpleNamespace(database_url="fixture"), kind=kind)
    assert selected == expected


@pytest.mark.parametrize("kind", ["other"])
def test_unsupported_worker_selection_fails_before_database_or_claim(monkeypatch, kind):
    import decagent.storage.database

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        decagent.storage.database,
        "Database",
        lambda url: pytest.fail("Database opened before startup protection"),
    )
    with pytest.raises((ValueError, RuntimeError), match="business|kind"):
        runtime_worker.run_worker(SimpleNamespace(database_url="fixture"), kind=kind)


def test_serial_loop_alternates_kinds_and_maintains_only_token_configured_repair():
    calls = []

    class Stop:
        def is_set(self):
            return len(calls) >= 6

        def wait(self, seconds):
            pytest.fail("Busy workers must not idle")

    def make(kind, token):
        return SimpleNamespace(
            kind=kind,
            settings=SimpleNamespace(github_token=token),
            run_once=lambda: calls.append(kind) or True,
            observe_merges=lambda: calls.append("maintenance:" + kind),
        )

    assert hasattr(runtime_worker, "_run_serial_workers"), (
        "Serial kind dispatcher is not implemented"
    )
    runtime_worker._run_serial_workers(
        [make("business", "token"), make("repository_repair", "")], Stop()
    )
    assert calls == ["business", "repository_repair"] * 3


def test_serial_loop_idles_after_every_kind_has_no_work():
    calls, waits = [], []

    class Stop:
        def is_set(self):
            return bool(waits)

        def wait(self, seconds):
            waits.append(seconds)

    workers = [
        SimpleNamespace(
            kind=kind,
            settings=SimpleNamespace(github_token=""),
            run_once=lambda kind=kind: calls.append(kind) and False,
        )
        for kind in ("business", "repository_repair")
    ]
    assert hasattr(runtime_worker, "_run_serial_workers"), (
        "Serial kind dispatcher is not implemented"
    )
    runtime_worker._run_serial_workers(workers, Stop(), idle_seconds=0.1)
    assert calls == ["business", "repository_repair"]
    assert waits == [0.1]
