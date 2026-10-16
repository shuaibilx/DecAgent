"""The rollout switch affects creation only; original graph tags stay immutable."""

import pytest

from decagent.business.registry import PluginRegistry
from decagent.config import Settings
from decagent.gateway.app import create_app
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import BusinessTaskInput
from decagent.runtime.config import RuntimeConfig
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def test_rollout_and_rollback_only_select_the_new_task_interpreter(database):
    backend = FixtureTickets(database)
    scope = PluginRegistry(database).list()[0]
    body = BusinessTaskInput(scope_id=str(scope["id"]), instruction="查看目录")
    saved = []
    for enabled in (False, True, False):
        control = BusinessControl(database, conditional_workflows_enabled=enabled)
        with database.connect() as conn:
            saved.append(
                control.submit_employee(
                    conn, body, backend.browser, backend.identity.credential_ref
                )
            )
    assert [row["graph_version"] for row in saved] == ["business-2", "business-3", "business-2"]
    assert database.get_task(saved[1]["id"])["graph_version"] == "business-3"


def test_application_passes_explicit_runtime_switch_to_control_plane(
    database, tmp_path, monkeypatch
):
    import decagent.gateway.app as module

    observed = []
    real = module.BusinessControl

    def spy(*args, **kwargs):
        observed.append(kwargs["conditional_workflows_enabled"])
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "BusinessControl", spy)
    create_app(
        database,
        Settings(data_dir=tmp_path),
        runtime_config=RuntimeConfig(conditional_workflows_enabled=True),
    )
    assert observed == [True]
