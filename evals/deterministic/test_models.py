import pytest
from langchain_core.messages import AIMessage, AIMessageChunk


class ScriptedClient:
    def __init__(self, response, chunks=()):
        self.response, self.chunks, self.bound = response, chunks, None

    def bind_tools(self, tools):
        self.bound = tools
        return self

    def invoke(self, messages):
        return self.response

    def stream(self, messages):
        yield from self.chunks


def test_multiple_tool_calls_are_normalized_without_executing_anything():
    from decagent.models.primary import PrimaryModel

    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"path": "a.py"}, "id": "read:1"},
            {"name": "diff", "args": {}, "id": "diff:2"},
        ],
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )
    client = ScriptedClient(response)
    turn = PrimaryModel(client).respond([{"role": "user", "content": "Find defect"}])
    assert [tool.id for tool in turn.tools] == ["read:1", "diff:2"]
    assert len(client.bound) == 8
    assert any(tool["function"]["name"] == "request_user" for tool in client.bound)
    assert turn.usage.total_tokens == 12
    assert turn.tool_result("read:1", {"content": "hello"})["tool_call_id"] == "read:1"
    with pytest.raises(ValueError, match="unknown"):
        turn.tool_result("invented", {})


def test_duplicate_tool_call_ids_and_unknown_tool_fail_closed():
    from decagent.models.primary import PrimaryModel

    for calls in [
        [{"name": "read_file", "args": {"path": "a.py"}, "id": "x"}] * 2,
        [{"name": "host_shell", "args": {}, "id": "x"}],
    ]:
        with pytest.raises(ValueError):
            PrimaryModel(ScriptedClient(AIMessage(content="", tool_calls=calls))).respond([])


def test_streamed_arguments_are_assembled_before_validation_and_usage_is_preserved():
    from decagent.models.primary import PrimaryModel

    chunks = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "read_file",
                    "args": '{"pa',
                    "id": "call1",
                    "index": 0,
                }
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": 'th":"file.py"}',
                    "id": None,
                    "index": 0,
                }
            ],
        ),
        AIMessageChunk(
            content="",
            usage_metadata={
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
            },
        ),
    ]
    turn = PrimaryModel(ScriptedClient(None, chunks)).respond([], stream=True)
    assert turn.tools[0].params["path"] == "file.py"
    assert turn.usage.total_tokens == 5
    with pytest.raises(ValueError, match="empty stream"):
        PrimaryModel(ScriptedClient(None)).respond([], stream=True)


def test_secrets_in_proposals_and_partial_stream_fail_without_tool_execution():
    from decagent.models.primary import PrimaryModel
    from decagent.policy import Redactor

    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "config.py", "content": "very-secret-value"},
                "id": "secret:1",
            }
        ],
    )
    with pytest.raises(ValueError, match="credentials"):
        PrimaryModel(
            ScriptedClient(response), Redactor(known_secrets=("very-secret-value",))
        ).respond([])
    chunks = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "read_file",
                    "args": '{"path":',
                    "id": "call1",
                    "index": 0,
                }
            ],
        )
    ]
    with pytest.raises(ValueError, match="Malformed|Missing required parameter"):
        PrimaryModel(ScriptedClient(None, chunks)).respond([], stream=True)
