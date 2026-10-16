from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from decagent.models.primary import PrimaryModel
from decagent.policy import (
    Action,
    ApprovalReply,
    CodeOrigin,
    CodeSnapshot,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    ReplyDecision,
    Source,
)

pytestmark = pytest.mark.integration


def test_postgres_approval_survives_graph_reassembly_without_duplicate_tool(database, tmp_path):
    from decagent.runtime.graph import build_graph
    from decagent.storage.approval_service import DurableApprovals

    db = database
    project = db.add_project("fixture/graph-approval")
    task = db.create_task(project["id"], "a" * 40, {})
    claimed = db.claim(task["id"], "worker")
    owner = Provenance(Source.OWNER, "owner", "session", True)
    auth = OwnerAuthorization(
        "auth", "1", str(task["id"]), str(project["id"]), project["repository"], owner
    )
    context = PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 40, "main", CodeOrigin.OWNER),
        auth,
        "1",
    )

    class Client:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if any(m["role"] == "tool" for m in messages):
                return AIMessage(content="Ready for independent verification")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "fixed.py", "content": "fixed"},
                        "id": "write1",
                    }
                ],
            )

    class Services:
        database = db
        model = PrimaryModel(Client())
        task_id = str(task["id"])
        lease_owner = "worker"
        generation = claimed["lease_generation"]
        policy_context = context
        approvals = DurableApprovals(db)
        results = []

        def current_context(self):
            return self.policy_context

        def invoke_model(self, messages):
            return self.model.respond(messages)

        def execute(self, action, record, execution_id):
            self.results.append(execution_id)
            return {"status": "succeeded", "evidence": "fixed"}

        def verify_and_finalize(self):
            return {"status": "unverified"}

    services = Services()
    config = {"configurable": {"thread_id": str(task["id"])}, "recursion_limit": 200}
    with db.checkpointer() as saver:
        graph = build_graph(services, saver)
        result = graph.invoke(
            {
                "task_id": str(task["id"]),
                "messages": [
                    {"role": "user", "content": "Repair fixture"},
                ],
                "iterations": 0,
                "queue": [],
            },
            config,
        )
        assert result["__interrupt__"]
        proposal = result["__interrupt__"][0].value
        assert not services.results
    row = services.approvals.read(proposal["approval_id"])
    services.approvals.reply(
        row.id,
        ApprovalReply(str(uuid4()), row.digest, ReplyDecision.APPROVE, owner),
        action=Action(result["action"]["tool"], result["action"]["params"]),
        context=context,
    )
    # Rebuild the graph against the same PostgreSQL thread/checkpoint after process restart.
    with db.checkpointer() as saver:
        resumed = build_graph(services, saver).invoke(
            Command(
                resume={
                    "approval_id": row.id,
                }
            ),
            config,
        )
    assert resumed["verification"]["status"] == "unverified"
    assert len(services.results) == 1
    assert db.get_task(task["id"])["state"] != "succeeded"
