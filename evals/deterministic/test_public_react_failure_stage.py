from types import SimpleNamespace

import pytest

from decagent.evaluation.business.routes import ControlledRoutes, ModelReply, ObservedUsage


class _Boundary:
    def check(self, _trial, *, initial):
        assert initial is False


class _Session:
    supports_user_simulation = True
    conversation = []
    spec = SimpleNamespace(commands={})

    def boundary(self):
        return _Boundary()

    def record_tau_step(self, _kind):
        return 1

    def ask_user(self, _prompt):
        raise ValueError("private simulator payload must not be retained")


def _reply(value):
    return ModelReply(
        value=value,
        tokens=1,
        cost_usd=None,
        model="test-model",
        input_tokens=1,
        output_tokens=0,
    )


def _run_react(session, action, *, max_output_bytes=128):
    return ControlledRoutes(lambda *_args: None)._react(
        session,
        SimpleNamespace(),
        {},
        SimpleNamespace(),
        ObservedUsage(),
        lambda _request: _reply(action),
        SimpleNamespace(max_output_bytes=max_output_bytes),
    )


def test_react_tags_simulated_user_boundary():
    with pytest.raises(ValueError) as caught:
        _run_react(_Session(), {"ask_user": "Could you clarify?"})

    assert caught.value.failure_stage == "tau_worker_user_simulator_step"


def test_react_tags_final_response_contract_failure():
    session = SimpleNamespace(
        supports_user_simulation=False,
        conversation=[],
        spec=SimpleNamespace(commands={}),
        boundary=lambda: _Boundary(),
    )

    with pytest.raises(ValueError) as caught:
        _run_react(
            session,
            {"finish": True, "message": "this exceeds the response bound"},
            max_output_bytes=8,
        )

    assert caught.value.failure_stage == "model_response_validation"
