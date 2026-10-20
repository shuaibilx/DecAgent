"""Contracts for isolated upstream EnterpriseOps infrastructure; no benchmark labels."""

import importlib.util
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest


def module():
    assert importlib.util.find_spec('decagent.evaluation.business.enterprise_environment'), (
        'EnterpriseOps isolated environment adapter is missing'
    )
    from decagent.evaluation.business import enterprise_environment
    return enterprise_environment


def fixture_spec(tmp_path):
    snapshot = tmp_path / 'snapshot.sql'
    snapshot.write_text('CREATE TABLE item (id INTEGER);', encoding='utf-8')
    return module().EnterpriseTrialSpec(
        task_id='task-fixture', domain='itsm',
        image='example/enterprise@sha256:' + 'a' * 64,
        platform='linux/amd64', snapshot_path=snapshot,
        snapshot_sha256=sha256(snapshot.read_bytes()).hexdigest(),
        system_prompt='Original system.', user_prompt='Original goal.',
        selected_tools=('get_item',), restricted_tools=(),
        server_name='gym-itsm-mcp', context={'x-itsm-user-token': 'fixture-identity'},
        verifiers=({'verifier_type': 'database_state', 'private': 'hidden-label'},),
    )


class DockerBoundary:
    def __init__(self):
        self.commands = []
        self.owner = None
        self.cid = 'a' * 64
        self.foreign = False
        self.fail_start = False

    def __call__(self, args):
        self.commands.append(args)
        if args[:2] == ['docker', 'create']:
            self.owner = args[args.index('--label') + 1].split('=', 1)[1]
            return self.cid
        if args[:2] == ['docker', 'start']:
            if self.fail_start:
                raise RuntimeError('start failure')
            return self.cid
        if args[:2] == ['docker', 'inspect']:
            return json.dumps([{
                'Id': self.cid,
                'Config': {'Labels': {module().OWNER_LABEL: (
                    'different-owner' if self.foreign else self.owner
                )}},
                'NetworkSettings': {'Ports': {'8005/tcp': [
                    {'HostIp': '127.0.0.1', 'HostPort': '42871'}
                ]}},
            }])
        if args[:3] == ['docker', 'rm', '--force']:
            return self.cid
        raise AssertionError(args)


def test_container_uses_pinned_image_dynamic_loopback_port_and_checked_cleanup():
    docker = DockerBoundary()
    with module().OwnedEnterpriseContainer(
        'example/gym@sha256:' + 'a' * 64, runner=docker,
    ) as container:
        assert container.base_url == 'http://127.0.0.1:42871'
        create = docker.commands[0]
        assert create[create.index('--publish') + 1] == '127.0.0.1::8005'
        assert create[create.index('--pull') + 1] == 'never'
        assert create[-1].endswith('@sha256:' + 'a' * 64)
        assert not any('volume' in arg or 'privileged' in arg for arg in create)
    assert docker.commands[-1] == ['docker', 'rm', '--force', docker.cid]
    assert docker.commands[-2] == ['docker', 'inspect', docker.cid]


def test_cleanup_refuses_foreign_container_and_start_failure_cleans_own():
    docker = DockerBoundary()
    container = module().OwnedEnterpriseContainer(
        'example/gym@sha256:' + 'a' * 64, runner=docker,
    )
    container.__enter__()
    docker.foreign = True
    with pytest.raises(RuntimeError, match='ownership'):
        container.close()
    assert not any(cmd[:2] == ['docker', 'rm'] for cmd in docker.commands)
    docker.foreign = False
    container.close()
    docker = DockerBoundary()
    docker.fail_start = True
    with pytest.raises(RuntimeError, match='start failure'):
        with module().OwnedEnterpriseContainer(
            'example/gym@sha256:' + 'a' * 64, runner=docker,
        ):
            pass
    assert docker.commands[-1] == ['docker', 'rm', '--force', docker.cid]


def test_environment_retains_owned_container_handle_when_cleanup_needs_retry(tmp_path):
    class RetryContainer:
        attempts = 0

        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError('temporary rm failure')

    container = RetryContainer()
    trial = module().EnterpriseEnvironment(fixture_spec(tmp_path))
    trial._container = container
    trial.database_id = 'db_original'
    trial._client = object()
    with pytest.raises(RuntimeError, match='rm failure'):
        trial.close()
    assert trial._container is container
    assert trial.database_id == 'db_original'
    with pytest.raises(RuntimeError, match='not active'):
        trial.model_input()
    trial.close()
    assert container.attempts == 2
    assert trial._container is None and trial.database_id is None


@pytest.mark.parametrize('image', ['latest', 'example:latest', 'example@sha256:nope'])
def test_mutable_images_are_rejected_before_docker_runs(image):
    docker = DockerBoundary()
    with pytest.raises(ValueError, match='digest'):
        module().OwnedEnterpriseContainer(image, runner=docker)
    assert docker.commands == []


def test_non_loopback_binding_is_rejected_and_owned_container_is_cleaned():
    class WrongBinding(DockerBoundary):
        def __call__(self, args):
            value = super().__call__(args)
            if args[:2] == ['docker', 'inspect']:
                return value.replace('127.0.0.1', '0.0.0.0')
            return value

    docker = WrongBinding()
    with pytest.raises(RuntimeError, match='loopback'):
        with module().OwnedEnterpriseContainer(
            'example/gym@sha256:' + 'a' * 64, runner=docker,
        ):
            pass
    assert docker.commands[-1] == ['docker', 'rm', '--force', docker.cid]


def test_failed_seed_cleans_owned_container_and_exposes_no_model_input(tmp_path):
    spec = fixture_spec(tmp_path)
    docker = DockerBoundary()

    def server(request):
        if request.method == 'GET':
            return httpx.Response(200)
        assert request.url.path == '/api/seed-database'
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        trial = module().EnterpriseEnvironment(spec, runner=docker, http=http)
        with pytest.raises(httpx.HTTPStatusError):
            with trial:
                pass
        with pytest.raises(RuntimeError, match='not active'):
            trial.model_input()
    assert docker.commands[-1] == ['docker', 'rm', '--force', docker.cid]


def test_wire_preserves_original_schema_arguments_result_and_identity_headers():
    requests = []
    schema = {
        'name': 'get_item', 'description': 'Original tool.',
        'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'integer'}}},
    }
    tool_result = {'content': [{'type': 'text', 'text': '{"id":"2"}'}], 'isError': True}

    def server(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        method = payload['method']
        if method == 'notifications/initialized':
            return httpx.Response(202)
        result = {'initialize': {'protocolVersion': '2024-11-05'},
                  'tools/list': {'tools': [schema]}, 'tools/call': tool_result}[method]
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'],
                                         'result': result},
                              headers={'mcp-session-id': 'upstream-session'})

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        wire = module().EnterpriseMCPClient(
            'http://127.0.0.1:42871', 'db_fixture',
            context={'itsm_user_token': 'fixture-identity'}, http=http,
        )
        wire.initialize()
        assert wire.list_tools() == [schema]
        assert wire.call_tool('get_item', {'id': '2', 'unknown': None}) == tool_result
    request, payload = requests[-1]
    assert payload['params'] == {'name': 'get_item', 'arguments': {'id': '2', 'unknown': None}}
    assert request.headers['x-database-id'] == 'db_fixture'
    assert request.headers['x-itsm-user-token'] == 'fixture-identity'
    assert request.headers['mcp-session-id'] == 'upstream-session'
    assert request.headers['accept'] == 'application/json, text/event-stream'


def test_wire_uses_the_supplied_tool_deadline_for_http():
    timeouts = []

    def server(request):
        timeouts.append(request.extensions["timeout"])
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        wire = module().EnterpriseMCPClient("http://127.0.0.1:42871", "db_fixture", http=http)
        assert wire.call_tool("lookup", {}, timeout_seconds=2.75) == {"content": []}

    assert len(timeouts) == 1
    assert timeouts[0] == {"connect": 2.75, "read": 2.75, "write": 2.75, "pool": 2.75}


def test_enterprise_environment_forwards_tool_deadline_to_wire_client():
    calls = []

    class Client:
        def call_tool(self, name, arguments, *, timeout_seconds):
            calls.append((name, arguments, timeout_seconds))
            return {"content": []}

    environment = object.__new__(module().EnterpriseEnvironment)
    environment._client = Client()
    environment._tools = [{"name": "lookup"}]

    assert environment.call_tool("lookup", {"id": "case-1"}, timeout_seconds=1.25) == {
        "content": []
    }
    assert calls == [("lookup", {"id": "case-1"}, 1.25)]


def test_private_database_fingerprint_hashes_live_schema_and_all_rows_deterministically():
    requests = []
    schema = [
        {"type": "table", "name": "items", "sql": "CREATE TABLE items(id INTEGER, name TEXT)"},
        {"type": "index", "name": "items_name", "sql": "CREATE INDEX items_name ON items(name)"},
    ]
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    def server(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        query = payload["query"]
        if query.startswith("SELECT type,name,sql FROM sqlite_master"):
            data = schema
        elif query.startswith('SELECT * FROM "items" ORDER BY rowid'):
            data = rows
        else:
            raise AssertionError(query)
        return httpx.Response(
            200,
            json={"success": True, "data": data, "row_count": len(data)},
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        wire = module().EnterpriseMCPClient(
            "http://127.0.0.1:42871", "db_fixture", http=http,
        )
        first = wire.state_fingerprint()
        second = wire.state_fingerprint()
    assert first == second
    assert first["table_count"] == 1
    assert first["row_count"] == 2
    assert len(first["digest"]) == 64
    assert all(payload["database_id"] == "db_fixture" for _, payload in requests)
    assert all(request.url.path == "/api/sql-runner" for request, _ in requests)


def test_wire_handles_jsonrpc_errors_without_inventing_success():
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, json={'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32601, 'message': 'missing'}}
    ))) as http:
        wire = module().EnterpriseMCPClient('http://127.0.0.1:1', 'db_test', http=http)
        with pytest.raises(module().EnterpriseProtocolError, match='missing'):
            wire.call_tool('original', {})


def test_trial_seeds_fresh_database_and_model_input_has_only_original_public_fields(tmp_path):
    spec = fixture_spec(tmp_path)
    requests = []
    catalog = [{'name': 'get_item', 'inputSchema': {'type': 'object'}},
               {'name': 'private_sql', 'inputSchema': {'type': 'object'}}]

    def server(request):
        if request.method == 'GET':
            return httpx.Response(200)
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == '/api/seed-database':
            return httpx.Response(200, json={'success': True})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(204)
        result = {'tools': catalog} if payload['method'] == 'tools/list' else {}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'result': result})

    docker = DockerBoundary()
    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        with module().EnterpriseEnvironment(spec, runner=docker, http=http) as trial:
            public = trial.model_input()
            assert public == {'system_prompt': 'Original system.', 'user_prompt': 'Original goal.',
                              'tools': catalog[:1]}
            public['tools'][0]['name'] = 'mutated'
            assert trial.model_input()['tools'][0]['name'] == 'get_item'
            assert 'hidden-label' not in repr(public)
            assert 'fixture-identity' not in repr(public)
            assert 'snapshot' not in repr(public)
            with pytest.raises(ValueError, match='available'):
                trial.call_tool('private_sql', {})
            first_id = trial.database_id
        with module().EnterpriseEnvironment(spec, runner=docker, http=http) as second:
            assert second.database_id != first_id
    seeds = [p for path, p in requests if path == '/api/seed-database']
    assert len(seeds) == 2
    assert seeds[0]['sql_content'] == spec.snapshot_path.read_text(encoding='utf-8')
    assert seeds[0]['database_id'] != seeds[1]['database_id']


def test_tampered_snapshot_is_rejected_without_starting_container(tmp_path):
    spec = fixture_spec(tmp_path)
    spec.snapshot_path.write_text('tampered', encoding='utf-8')
    docker = DockerBoundary()
    with pytest.raises(ValueError, match='snapshot'):
        with module().EnterpriseEnvironment(spec, runner=docker):
            pass
    assert docker.commands == []


@pytest.mark.asyncio
async def test_original_scorer_hook_receives_private_configs_and_original_results(tmp_path):
    spec = fixture_spec(tmp_path)
    calls = []
    result = {'passed': False, 'actual': 0, 'expected': 1}
    original_config = SimpleNamespace(gym_name=spec.server_name)
    wire = SimpleNamespace(database_id='db_private', context=deepcopy(spec.context))

    class OriginalEngine:
        async def execute_verifier(self, config, response, database_id, **kwargs):
            calls.append((config, response, database_id, kwargs))
            return result

    hook = module().UpstreamScorerHook(wire, spec.server_name)
    response = {'final_answer': 'Original answer.'}
    returned = await hook.run(OriginalEngine(), [original_config], response)
    assert returned == [result]
    assert returned[0] is result
    assert calls == [(original_config, response, 'db_private', {
        'context': spec.context, 'gym_name': spec.server_name,
    })]


def test_pinned_row_factory_resolves_only_manifest_snapshot_and_never_rewrites_task(tmp_path):
    spec = fixture_spec(tmp_path)
    row = {
        'task_id': spec.task_id, 'domain': 'itsm',
        'system_prompt': spec.system_prompt, 'user_prompt': spec.user_prompt,
        'selected_tools': ['get_item'], 'restricted_tools': ['private_sql'],
        'mcp_endpoint': '/mcp',
        'gym_servers_config': json.dumps([{
            'mcp_server_name': spec.server_name, 'mcp_server_url': 'http://localhost:8006',
            'seed_database_file': 'snapshots/snapshot.sql', 'context': spec.context,
        }]),
        'verifiers': json.dumps(list(spec.verifiers)),
    }
    manifest = {'enterpriseops_gym': {
        'mcp_environment': {'endpoint': '/mcp', 'images': [{
            'image': 'example/enterpriseops-gym-mcp-itsm',
            'manifest_digest': 'sha256:' + 'a' * 64, 'platform': 'linux/amd64',
        }]},
        'database_snapshots': {'referenced_snapshot_sha256': [{
            'domain': 'itsm', 'path': 'snapshot.sql', 'sha256': spec.snapshot_sha256,
        }]},
    }}
    # The configured path must identify the exact path recorded in the manifest.
    row_config = json.loads(row['gym_servers_config'])
    row_config[0]['seed_database_file'] = 'snapshot.sql'
    row['gym_servers_config'] = json.dumps(row_config)
    original = deepcopy(row)
    loaded = module().spec_from_pinned_row(tmp_path, row, manifest)
    assert loaded.snapshot_path == spec.snapshot_path
    assert loaded.context == spec.context
    assert loaded.verifiers == spec.verifiers
    assert loaded.user_prompt == spec.user_prompt
    assert row == original
    row_config[0]['seed_database_file'] = '../snapshot.sql'
    row['gym_servers_config'] = json.dumps(row_config)
    with pytest.raises(ValueError, match='snapshot'):
        module().spec_from_pinned_row(tmp_path, row, manifest)


def test_wire_accepts_actual_sse_response_and_refuses_wrong_jsonrpc_id():
    def server(request):
        payload = json.loads(request.content)
        data = json.dumps({'jsonrpc': '2.0', 'id': payload['id'],
                           'result': {'content': [{'type': 'text', 'text': 'unchanged'}]}})
        return httpx.Response(200, text='event: message\ndata: ' + data + '\n\n',
                              headers={'content-type': 'text/event-stream'})
    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        wire = module().EnterpriseMCPClient('http://127.0.0.1:1', 'db_test', http=http)
        assert wire.call_tool('original', {}) == {
            'content': [{'type': 'text', 'text': 'unchanged'}]
        }
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, json={'jsonrpc': '2.0', 'id': 999, 'result': {}}
    ))) as http:
        wire = module().EnterpriseMCPClient('http://127.0.0.1:1', 'db_test', http=http)
        with pytest.raises(module().EnterpriseProtocolError, match='Mismatched'):
            wire.call_tool('original', {})
