from pathlib import Path

import pytest

from decagent.executor.broker import Broker

pytestmark = pytest.mark.integration


def test_stop_invalidates_execution_state_and_rejects_stale_generation(database, tmp_path):
    from decagent.executor.protocol import TaskControlRequest

    project = database.add_project("fixture/control")
    task = database.create_task(project["id"], "a" * 40, {})
    claim = database.claim(task["id"], "worker")

    class Engine:
        proxy_container = "fixture-proxy"

        def api(self, method, path, **kwargs):
            assert method == "GET" and path == "/containers/json"
            return []

    broker = Broker(database, Engine(), Path(tmp_path))
    request = TaskControlRequest(
        task_id=str(task["id"]), owner="worker", lease_generation=claim["lease_generation"]
    )
    result = broker.stop_task(request)
    assert result["stopped"] is True
    assert database.get_task(task["id"])["state"] == "recovering"
    with pytest.raises(ValueError, match="stale"):
        database.assert_lease(task["id"], "worker", claim["lease_generation"])
    with pytest.raises(ValueError, match="generation"):
        broker.stop_task(request.model_copy(update={"lease_generation": 999}))
