"""Local management remains available without a business execution CLI."""

from decagent.cli import main


def test_business_worker_selection_and_retired_owner_submit(monkeypatch):
    import pytest

    import decagent.runtime.worker
    from decagent.config import Settings

    selected = []
    monkeypatch.setattr(Settings, "from_env", lambda: Settings())
    monkeypatch.setattr(
        decagent.runtime.worker, "run_worker", lambda settings, *, kind=None: selected.append(kind)
    )
    main(["worker", "--kind", "business"])
    assert selected == ["business"]
    with pytest.raises(SystemExit) as exc:
        main(["business", "submit"])
    assert exc.value.code == 2


def test_plugin_inspection_returns_the_registered_contract_even_when_disabled(monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from uuid import UUID

    import decagent.business.administration as administration
    from decagent.config import Settings

    scope_id = "00000000-0000-0000-0000-000000000001"
    inspected = []
    monkeypatch.setattr(Settings, "from_env", lambda: Settings())
    monkeypatch.setattr(
        administration,
        "Database",
        lambda url: SimpleNamespace(check_version=lambda *args, **kwargs: None),
    )

    class Registry:
        def __init__(self, database):
            pass

        def inspect(self, identifier):
            inspected.append(identifier)
            return {"id": scope_id, "enabled": False, "manifest": {"commands": {"read": {}}}}

    monkeypatch.setattr(administration, "PluginRegistry", Registry)
    main(["plugins", "inspect", scope_id])
    assert inspected == [UUID(scope_id)]
    assert json.loads(capsys.readouterr().out) == {
        "id": scope_id,
        "enabled": False,
        "manifest": {"commands": {"read": {}}},
    }


def test_business_accounts_seed_uses_local_management_and_prints_no_password(
    monkeypatch, capsys, tmp_path
):
    import json

    import decagent.business.administration as administration
    from decagent.config import Settings

    settings = Settings(database_url="postgresql://local/test", data_dir=tmp_path / ".decagent")
    seeded = []

    class Database:
        def __init__(self, url):
            assert url == settings.database_url

        def check_version(self, graph_version, *, task_kind):
            assert (graph_version, task_kind) == ("business-2", "business")

    def seed(_, credential_dir):
        seeded.append(credential_dir)
        return {"created": ["employee-it"], "preserved": []}

    monkeypatch.setattr(Settings, "from_env", lambda: settings)
    monkeypatch.setattr(administration, "Database", Database)
    monkeypatch.setattr(administration, "seed_employee_accounts", seed, raising=False)
    main(["business", "accounts", "seed"])

    assert seeded == [settings.data_dir / "demo-auth"]
    assert json.loads(capsys.readouterr().out) == {
        "created": ["employee-it"],
        "preserved": [],
    }
