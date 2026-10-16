"""Executor contracts run without Docker or external services."""

import asyncio
import importlib
import io
import tarfile
from uuid import uuid4

import pytest


def module(name):
    assert importlib.util.find_spec(f"decagent.executor.{name}"), f"Missing executor {name}"
    return importlib.import_module(f"decagent.executor.{name}")


def request(**changes):
    value = {
        "task_id": str(uuid4()),
        "action_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "action_digest": "a" * 64,
        "owner": "worker-one",
        "lease_generation": 1,
        "argv": ["python", "-c", "print('ok')"],
        "cwd": ".",
        "kind": "command",
    }
    return value | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"env": {"TOKEN": "secret"}},
        {"docker_args": ["--privileged"]},
        {"host_path": "C:/Users/example-user"},
        {"cwd": "../other"},
        {"cwd": "/etc"},
        {"lease_generation": 0},
        {"lease_generation": True},
        {"argv": "python"},
        {"argv": []},
        {"kind": "interactive"},
        {"timeout": 800},
    ],
)
def test_protocol_rejects_unbounded_parameters(changes):
    protocol = module("protocol")
    with pytest.raises(ValueError):
        protocol.ExecutionRequest.model_validate(request(**changes))


def test_protocol_timeouts_are_fixed_and_generation_is_bound():
    protocol = module("protocol")
    normal = protocol.ExecutionRequest.model_validate(request())
    setup = protocol.ExecutionRequest.model_validate(request(kind="setup"))
    assert (normal.timeout_seconds, setup.timeout_seconds) == (120, 600)
    assert normal.payload_hash != normal.model_copy(update={"lease_generation": 2}).payload_hash


def archive(*members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as stream:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "../../host"
            stream.addfile(info, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/etc/key", tarfile.REGTYPE),
        ("C:/key", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard", tarfile.LNKTYPE),
        ("fifo", tarfile.FIFOTYPE),
        ("device", tarfile.CHRTYPE),
        (".env", tarfile.REGTYPE),
        (".git/config", tarfile.REGTYPE),
    ],
)
def test_archive_rejects_escape_links_specials_and_secrets(name, kind):
    safe = module("archive")
    with pytest.raises(ValueError):
        safe.read_archive(archive((name, b"secret", kind)))


def test_archive_bounds_size_and_duplicate_entries():
    safe = module("archive")
    with pytest.raises(ValueError):
        safe.read_archive(archive(("a", b"x", tarfile.REGTYPE)), max_bytes=0)
    with pytest.raises(ValueError):
        safe.read_archive(archive(("a", b"x", tarfile.REGTYPE), ("a", b"y", tarfile.REGTYPE)))
    assert safe.read_archive(archive(("pkg/main.py", b"print(1)", tarfile.REGTYPE))) == {
        "pkg/main.py": b"print(1)"
    }


def test_diff_uses_immutable_baseline_content():
    safe = module("archive")
    before = {"main.py": b"a\n", "deleted.py": b"old\n"}
    after = {"main.py": b"b\n", "new.py": b"new\n"}
    diff = safe.workspace_diff(before, after)
    assert "-a\n+b" in diff and "deleted.py" in diff and "new.py" in diff
    assert before["main.py"] == b"a\n"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:8.8.8.8",
        "64:ff9b::a00:1",
        "2002:0a00:0001::1",
    ],
)
def test_proxy_denies_private_and_transition_addresses(address):
    proxy = module("proxy")
    with pytest.raises(ValueError):
        proxy.validate_address(address)


def test_proxy_public_ip_and_each_dns_answer_are_validated():
    proxy = module("proxy")
    assert proxy.validate_address("8.8.8.8") == "8.8.8.8"
    assert proxy.validate_address("2606:4700:4700::1111") == "2606:4700:4700::1111"

    async def exercise():
        answers = iter([["8.8.8.8"], ["127.0.0.1"], ["8.8.8.8", "10.0.0.1"]])

        async def resolver(host, port):
            return next(answers)

        policy = proxy.TargetPolicy(resolver=resolver)
        assert await policy.resolve("example.org", 443) == ["8.8.8.8"]
        for _ in range(2):
            with pytest.raises(ValueError):
                await policy.resolve("example.org", 443)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file",
        "http://user:pass@example.org",
        "http://host.docker.internal/",
        "http://postgres/",
        "http://example.org:22/",
        "http://localhost./",
        "http://metadata/",
    ],
)
def test_proxy_rejects_protocols_ports_credentials_and_control_names(url):
    proxy = module("proxy")
    with pytest.raises(ValueError):
        proxy.parse_target(url)


def test_receipt_duplicate_conflict_and_crash_never_restart(tmp_path):
    journal = module("receipts").ReceiptStore(tmp_path)
    value = module("protocol").ExecutionRequest.model_validate(request())
    first, created = journal.reserve(value)
    assert created and first["state"] == "reserved"
    same, created = journal.reserve(value)
    assert not created and same == first
    changed = value.model_copy(update={"argv": ["python", "-V"]})
    with pytest.raises(ValueError):
        journal.reserve(changed)
    journal.update(value.execution_id, state="starting")
    reopened = module("receipts").ReceiptStore(tmp_path)
    receipt, created = reopened.reserve(value)
    assert not created and receipt["state"] == "starting"


def test_docker_config_has_only_task_volumes_fixed_limits_and_no_secrets():
    docker = module("docker")
    image = "sha256:" + "b" * 64
    engine = docker.DockerEngine(runner_image=image, proxy_container="egress-proxy")
    value = module("protocol").ExecutionRequest.model_validate(request())
    config = engine.command_config(value)
    host = config["HostConfig"]
    assert config["User"] == "10001:10001"
    assert host["NanoCpus"] == 2_000_000_000 and host["Memory"] == 4 * 1024**3
    assert host["PidsLimit"] == 128 and host["CapDrop"] == ["ALL"]
    assert host["ReadonlyRootfs"] and host["SecurityOpt"] == ["no-new-privileges:true"]
    assert not host.get("Binds") and all(m["Type"] == "volume" for m in host["Mounts"])
    assert all("TOKEN" not in item and "KEY" not in item for item in config["Env"])
    assert host["NetworkMode"].startswith("decagent-task-")


def test_broker_api_requires_internal_auth_and_fails_closed():
    from fastapi.testclient import TestClient

    broker_module = module("broker")

    class Broker:
        ready = False

    app = broker_module.create_app(Broker(), "x" * 32)
    client = TestClient(app)
    assert client.get("/health").status_code == 401
    result = client.get("/health", headers={"Authorization": "Bearer " + "x" * 32})
    assert result.status_code == 503


def test_broker_exact_action_binding_rejects_changed_argv_timeout_and_setup():
    broker = module("broker")
    value = module("protocol").ExecutionRequest.model_validate(request())
    snapshot = {
        "tool": "run_command",
        "params": {
            "argv": value.argv,
            "cwd": ".",
            "timeout_seconds": 120,
            "max_output_bytes": 65536,
        },
    }
    broker.match_action(value, snapshot)
    for change in [
        {"argv": ["python", "-V"]},
        {"timeout_seconds": 20},
        {"kind": "setup", "timeout_seconds": 600},
    ]:
        with pytest.raises(ValueError):
            broker.match_action(value.model_copy(update=change), snapshot)


def test_boundary_failed_probe_never_enables_commands(tmp_path):
    broker = module("broker")

    class Engine:
        def initialize(self):
            raise ValueError("network failure")

    value = broker.Broker(None, Engine(), tmp_path)
    with pytest.raises(ValueError):
        value.initialize()
    assert not value.ready
    with pytest.raises(ValueError, match="boundary"):
        value.execute(module("protocol").ExecutionRequest.model_validate(request()))


def test_proxy_pins_literal_address_instead_of_second_dns_resolution(monkeypatch):
    proxy = module("proxy")
    calls = []

    async def resolver(host, port):
        return ["8.8.8.8"]

    async def connection(host, port):
        calls.append((host, port))
        return ("reader", "writer")

    monkeypatch.setattr(asyncio, "open_connection", connection)
    assert asyncio.run(proxy.TargetPolicy(resolver).connect("example.org", 443)) == (
        "reader",
        "writer",
    )
    assert calls == [("8.8.8.8", 443)]


def test_workspace_receipt_is_durable_and_baseline_cannot_be_replaced(tmp_path):
    store = module("receipts").ReceiptStore(tmp_path)
    task_id = str(uuid4())
    assert store.workspace_reserve(task_id, "a" * 64) == {"digest": "a" * 64, "state": "creating"}
    reopened = module("receipts").ReceiptStore(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        reopened.workspace_reserve(task_id, "b" * 64)
    assert reopened.workspace_read(task_id)["state"] == "creating"
    reopened.workspace_update(task_id, state="verified")
    assert reopened.workspace_reserve(task_id, "a" * 64)["state"] == "verified"


def test_terminal_receipt_cannot_be_overwritten_by_cancel_completion_race(tmp_path):
    store = module("receipts").ReceiptStore(tmp_path)
    value = module("protocol").ExecutionRequest.model_validate(request())
    store.reserve(value)
    store.finish(value.execution_id, state="cancelled", output="cancelled")
    assert store.finish(value.execution_id, state="completed", output="late completion")[
        "state"
    ] == ("cancelled")


def test_snapshot_api_cannot_hide_credentials_in_base64():
    broker = module("broker")
    with pytest.raises(ValueError, match="credential"):
        broker.snapshot_response({"main.py": b"key = 'sk-abcdefghijklmnopqrstuv'\n"})
    response = broker.snapshot_response({"main.py": b"print('safe')\n"})
    assert response["workspace_hash"] and response["archive_base64"]


def test_invalid_broker_requests_do_not_echo_secrets():
    from fastapi.testclient import TestClient

    broker_module = module("broker")

    class Broker:
        ready = False

    client = TestClient(broker_module.create_app(Broker(), "x" * 32))
    result = client.post(
        "/v1/executions",
        json=request(env={"TOKEN": "secret-should-not-echo"}),
        headers={"Authorization": "Bearer " + "x" * 32},
    )
    assert result.status_code == 422
    assert "secret-should-not-echo" not in result.text
    token_field = "ghp_" + "a" * 30
    result = client.post(
        "/v1/executions",
        json=request(**{token_field: "x"}),
        headers={"Authorization": "Bearer " + "x" * 32},
    )
    assert token_field not in result.text
