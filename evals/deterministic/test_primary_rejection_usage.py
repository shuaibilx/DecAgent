"""Completed, rejected primary responses still owe their independently valid usage."""

import traceback
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from decagent.models.primary import ModelUsage, PrimaryModel, _response_usage, tool_schemas
from decagent.policy import Redactor
from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice
from decagent.storage.budget import Budget, BudgetExceeded

USAGE = {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}
ACTUAL_MODEL = "Vendor/Resolved.Model-2026:free"
SYNTHETIC_MARKER = "SYNTHETIC-PRIVATE-MARKER-KEEP-OUT"
KNOWN_PRICE = TokenPrice("fixture-v1", Decimal("2"), Decimal("4"))
FREE_PRICE = TokenPrice("owner-free-v1", Decimal("0"), Decimal("0"))
UNKNOWN_RESERVATION = Decimal("0.25")


def test_response_usage_retains_and_bounds_cached_input_tokens():
    usage, invalid = _response_usage(
        {
            **USAGE,
            "input_token_details": {"cache_read": 4},
        }
    )

    assert invalid is False
    assert usage == ModelUsage(11, 3, 14, cached_input_tokens=4)

    invalid_usage, invalid = _response_usage(
        {
            **USAGE,
            "input_token_details": {"cache_read": 12},
        }
    )
    assert invalid is True
    assert invalid_usage is None


def test_response_usage_accepts_qwen_openai_prompt_token_details_and_rejects_conflicts():
    usage, invalid = _response_usage(
        {
            **USAGE,
            "prompt_tokens_details": {"cached_tokens": 4},
        }
    )

    assert invalid is False
    assert usage == ModelUsage(11, 3, 14, cached_input_tokens=4)

    conflicting, invalid = _response_usage(
        {
            **USAGE,
            "input_token_details": {"cache_read": 4},
            "prompt_tokens_details": {"cached_tokens": 5},
        }
    )
    assert invalid is True
    assert conflicting is None


class Client:
    """Only the provider boundary is replaced; normalization and budgeting stay real."""

    def __init__(self, response=None, *, chunks=(), failure=None, stream_failure=None):
        self.response = response
        self.chunks = chunks
        self.failure = failure
        self.stream_failure = stream_failure
        self.model_name = "requested/model"
        self.dispatches = 0

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.dispatches += 1
        if self.failure is not None:
            raise self.failure
        return self.response

    def stream(self, messages):
        self.dispatches += 1
        yield from self.chunks
        if self.stream_failure is not None:
            raise self.stream_failure


class Ledger:
    """The real Budget retains unknown reservations without a database dependency."""

    def __init__(self, budget=None):
        self.budget = budget or Budget(max_calls=4, max_tokens=200_000, max_cost=Decimal("5"))
        self.receipts = []
        self.settlements = []

    def reserve(self, key, attempt, provider, tokens, cost):
        receipt = self.budget.reserve(tokens, cost)
        self.receipts.append(receipt)
        return receipt

    def settle(self, receipt, tokens, cost):
        self.settlements.append((tokens, cost))
        self.budget.settle(receipt, tokens, cost)

    @property
    def entry(self):
        assert len(self.receipts) == 1
        return self.budget.reservations[self.receipts[0]]


def response(kind="schema", *, usage=USAGE, model=ACTUAL_MODEL):
    calls = [{"name": "read_file", "args": {"path": 7}, "id": "call-1"}]
    content = ""
    invalid_calls = []
    if kind == "unknown_tool":
        calls = [{"name": "unregistered_tool", "args": {}, "id": "call-1"}]
    elif kind == "duplicate_ids":
        calls = [{"name": "read_file", "args": {"path": "a.py"}, "id": "same"}] * 2
    elif kind == "missing_id":
        calls = [{"name": "read_file", "args": {"path": "a.py"}, "id": None}]
    elif kind == "invalid_tool_calls":
        calls = []
        invalid_calls = [
            {
                "name": "read_file",
                "args": '{"path":',
                "id": "bad-call",
                "error": SYNTHETIC_MARKER,
            }
        ]
    elif kind == "content_type":
        calls = []
        content = [{"type": "text", "text": SYNTHETIC_MARKER}]
    elif kind == "last_tool_invalid":
        calls.insert(0, {"name": "read_file", "args": {"path": "a.py"}, "id": "valid"})
    elif kind == "unsafe_tool_proposal":
        calls = [
            {
                "name": "write_file",
                "args": {"path": "config.py", "content": SYNTHETIC_MARKER},
                "id": "unsafe",
            }
        ]
    elif kind == "valid":
        calls = [{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}]
    result = AIMessage(
        content=content,
        tool_calls=calls,
        invalid_tool_calls=invalid_calls,
        usage_metadata=dict(USAGE),
        response_metadata={"model_name": model},
    )
    # Preserve hostile wire values instead of letting Pydantic coerce them first.
    return result.model_copy(update={"usage_metadata": usage})


def wrapped(client, *, price_kind="known", ledger=None, redactor=None, **kwargs):
    ledger = ledger or Ledger()
    price = {"known": KNOWN_PRICE, "owner_free": FREE_PRICE, "unknown": None}[price_kind]
    model = PrimaryModel(client, redactor or Redactor(known_secrets=(SYNTHETIC_MARKER,)))
    return (
        BudgetedPrimary(
            model,
            ledger,
            price=price,
            unknown_cost_reservation=UNKNOWN_RESERVATION if price is None else None,
            **kwargs,
        ),
        ledger,
    )


def assert_unknown(ledger):
    assert ledger.settlements == [(None, None)]
    assert ledger.entry.actual_tokens is None
    assert ledger.entry.actual_cost is None
    assert ledger.entry.status == "unknown"
    assert ledger.budget.reserved_tokens == ledger.entry.tokens
    assert ledger.budget.reserved_cost == ledger.entry.cost


@pytest.mark.parametrize("price_kind", ["known", "owner_free", "unknown"])
@pytest.mark.parametrize(
    "kind,reason",
    [
        ("schema", "tool_schema"),
        ("unknown_tool", "tool_schema"),
        ("duplicate_ids", "tool_identity"),
        ("missing_id", "tool_identity"),
        ("invalid_tool_calls", "invalid_tool_calls"),
        ("content_type", "content_type"),
        ("last_tool_invalid", "tool_schema"),
        ("unsafe_tool_proposal", "unsafe_tool_proposal"),
    ],
)
def test_completed_rejection_keeps_actual_usage_without_returning_partial_tools(
    kind, reason, price_kind
):
    client = Client(response(kind))
    primary, ledger = wrapped(client, price_kind=price_kind)
    returned = []

    with pytest.raises(ValueError) as caught:
        returned.append(primary.respond([]))

    # This must go RED on lost ledger actuals, not an import of a future exception.
    assert ledger.entry.actual_tokens == 14
    expected_cost = (
        {"known": KNOWN_PRICE, "owner_free": FREE_PRICE}[price_kind].cost(11, 3)
        if price_kind != "unknown"
        else None
    )
    assert ledger.entry.actual_cost == expected_cost
    assert ledger.settlements == [(14, expected_cost)]
    assert ledger.entry.status == ("unknown" if price_kind == "unknown" else "settled")
    if price_kind == "unknown":
        assert ledger.budget.reserved_cost == UNKNOWN_RESERVATION
        assert ledger.budget.reserved_tokens == ledger.entry.tokens
    else:
        assert ledger.budget.spent_tokens == 14
        assert ledger.budget.spent_cost == expected_cost
        assert ledger.budget.reserved_cost == 0
        assert ledger.budget.reserved_tokens == 0
    assert client.dispatches == 1
    assert returned == []
    assert type(caught.value).__name__ == "PrimaryResponseRejected"
    assert getattr(caught.value, "usage", None) == ModelUsage(11, 3, 14)
    assert getattr(caught.value, "model", None) == ACTUAL_MODEL
    assert getattr(caught.value, "reason", None) == reason
    for field in ("response", "params", "tools", "proposals", "original"):
        assert getattr(caught.value, field, None) is None


@pytest.mark.parametrize("price_kind", ["known", "owner_free", "unknown"])
def test_zero_token_completed_rejection_is_known_usage(price_kind):
    client = Client(response(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
    primary, ledger = wrapped(client, price_kind=price_kind)

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert ledger.entry.actual_tokens == 0
    assert ledger.entry.actual_cost == (None if price_kind == "unknown" else Decimal("0"))
    assert ledger.entry.status == ("unknown" if price_kind == "unknown" else "settled")
    assert getattr(caught.value, "usage", None) == ModelUsage(0, 0, 0)


INVALID_USAGE = [
    pytest.param(None, id="absent"),
    pytest.param({}, id="empty"),
    pytest.param({"input_tokens": 11, "output_tokens": 3}, id="missing-total"),
    pytest.param({"output_tokens": 3, "total_tokens": 14}, id="missing-input"),
    pytest.param({"input_tokens": 11, "total_tokens": 14}, id="missing-output"),
    pytest.param({"input_tokens": -1, "output_tokens": 3, "total_tokens": 2}, id="negative"),
    pytest.param({"input_tokens": True, "output_tokens": 3, "total_tokens": 4}, id="bool"),
    pytest.param(
        {"input_tokens": 11, "output_tokens": False, "total_tokens": 11}, id="bool-output"
    ),
    pytest.param({"input_tokens": 1.0, "output_tokens": 3, "total_tokens": 4}, id="float"),
    pytest.param({"input_tokens": "11", "output_tokens": 3, "total_tokens": 14}, id="string"),
    pytest.param({"input_tokens": 11, "output_tokens": 3, "total_tokens": 99}, id="inconsistent"),
]


@pytest.mark.parametrize("usage", INVALID_USAGE)
def test_absent_or_invalid_usage_on_rejected_proposal_stays_unknown(usage):
    primary, ledger = wrapped(Client(response(usage=usage)))

    with pytest.raises(ValueError):
        primary.respond([])

    assert_unknown(ledger)


@pytest.mark.parametrize("usage", INVALID_USAGE[2:])
def test_invalid_usage_rejects_otherwise_valid_turn_without_guessing_actuals(usage):
    primary, ledger = wrapped(Client(response("valid", usage=usage)))

    with pytest.raises(ValueError):
        primary.respond([])

    assert_unknown(ledger)


@pytest.mark.parametrize("usage", [None, {}])
def test_success_without_usage_keeps_reservation(usage):
    primary, ledger = wrapped(Client(response("valid", usage=usage)))

    turn = primary.respond([])

    assert turn.usage is None
    assert len(turn.tools) == 1
    assert_unknown(ledger)


def test_usage_validation_rejection_has_fixed_reason_and_no_invalid_usage():
    primary, ledger = wrapped(
        Client(
            response("valid", usage={"input_tokens": True, "output_tokens": 3, "total_tokens": 4})
        )
    )

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert_unknown(ledger)
    assert type(caught.value).__name__ == "PrimaryResponseRejected"
    assert getattr(caught.value, "usage", None) is None
    assert getattr(caught.value, "reason", None) == "usage_invalid"


def invalid_stream_chunks(*, usage_at_end=True):
    proposal = AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": "read_file", "args": '{"path":7}', "id": "call-1", "index": 0}],
        usage_metadata=None if usage_at_end else dict(USAGE),
    )
    terminal = AIMessageChunk(
        content="",
        usage_metadata=dict(USAGE) if usage_at_end else None,
        response_metadata={"model_name": ACTUAL_MODEL},
    )
    return [proposal, terminal]


@pytest.mark.parametrize("usage_at_end", [False, True])
def test_normally_exhausted_stream_rejection_settles_complete_usage(usage_at_end):
    client = Client(chunks=invalid_stream_chunks(usage_at_end=usage_at_end))
    primary, ledger = wrapped(client)

    with pytest.raises(ValueError) as caught:
        primary.respond([], stream=True)

    assert ledger.entry.actual_tokens == 14
    assert ledger.entry.actual_cost == KNOWN_PRICE.cost(11, 3)
    assert ledger.entry.status == "settled"
    assert getattr(caught.value, "usage", None) == ModelUsage(11, 3, 14)
    assert getattr(caught.value, "model", None) == ACTUAL_MODEL
    assert getattr(caught.value, "reason", None) == "tool_schema"
    assert client.dispatches == 1


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_interrupted_stream_does_not_trust_even_observed_usage(error_type):
    failure = error_type("Injected interruption after a usage-bearing chunk")
    failure.usage = ModelUsage(11, 3, 14)
    failure.model = ACTUAL_MODEL
    failure.reason = "tool_schema"
    client = Client(chunks=invalid_stream_chunks(), stream_failure=failure)
    primary, ledger = wrapped(client)

    with pytest.raises(error_type) as caught:
        primary.respond([], stream=True)

    assert caught.value is failure
    assert_unknown(ledger)
    assert client.dispatches == 1


def test_unsupported_chunk_before_stream_exhaustion_stays_unknown():
    client = Client(chunks=[*invalid_stream_chunks(), {"usage_metadata": dict(USAGE)}])
    primary, ledger = wrapped(client)

    with pytest.raises(ValueError):
        primary.respond([], stream=True)

    assert_unknown(ledger)
    assert client.dispatches == 1


def test_normally_exhausted_empty_stream_has_no_usage_to_reconcile():
    primary, ledger = wrapped(Client(chunks=[]))

    with pytest.raises(ValueError) as caught:
        primary.respond([], stream=True)

    assert_unknown(ledger)
    assert getattr(caught.value, "reason", None) == "empty_stream"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_generic_provider_error_cannot_forge_response_usage(error_type):
    failure = error_type("Injected provider failure")
    failure.usage = ModelUsage(11, 3, 14)
    failure.model = ACTUAL_MODEL
    failure.reason = "tool_schema"
    failure.dispatched = False
    client = Client(failure=failure)
    primary, ledger = wrapped(client)

    with pytest.raises(error_type) as caught:
        primary.respond([])

    assert caught.value is failure
    assert_unknown(ledger)
    assert client.dispatches == 1


def test_non_message_provider_result_cannot_supply_trusted_usage():
    client = Client(
        {"usage_metadata": dict(USAGE), "response_metadata": {"model_name": ACTUAL_MODEL}}
    )
    primary, ledger = wrapped(client)

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert_unknown(ledger)
    assert getattr(caught.value, "usage", None) is None
    assert client.dispatches == 1


def test_before_dispatch_guard_releases_reservation_without_calling_provider():
    client = Client(response("valid"))
    failure = ValueError("Injected stale authorization guard")

    def guard():
        raise failure

    primary, ledger = wrapped(client, before_dispatch=guard)

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert caught.value is failure
    assert client.dispatches == 0
    assert ledger.settlements == [(0, Decimal("0"))]
    assert ledger.entry.status == "settled"
    assert ledger.budget.reserved_tokens == ledger.budget.reserved_cost == 0


@pytest.mark.parametrize("limit", ["calls", "tokens", "cost"])
def test_exhausted_budget_does_not_dispatch_or_settle_unreserved_request(limit):
    budget = Budget(
        max_calls=1,
        max_tokens=1 if limit == "tokens" else 200_000,
        max_cost=Decimal("0.000001") if limit == "cost" else Decimal("5"),
    )
    if limit == "calls":
        budget.calls = 1
    ledger = Ledger(budget)
    client = Client(response("valid"))
    primary, _ = wrapped(client, ledger=ledger)

    with pytest.raises(BudgetExceeded):
        primary.respond([])

    assert client.dispatches == 0
    assert ledger.receipts == ledger.settlements == []


def test_missing_price_and_unknown_fee_bound_does_not_dispatch():
    client = Client(response("valid"))
    ledger = Ledger()
    primary = BudgetedPrimary(PrimaryModel(client), ledger)

    with pytest.raises(BudgetExceeded, match="price"):
        primary.respond([])

    assert client.dispatches == 0
    assert ledger.receipts == ledger.settlements == []


@pytest.mark.parametrize(
    "messages,redactor,reason",
    [
        ([{"role": "user", "content": "a"}] * 2, Redactor(max_items=1), "context_limit"),
        ([{"role": "user", "content": {1: SYNTHETIC_MARKER}}], Redactor(), "input_invalid"),
    ],
)
def test_local_preflight_rejection_releases_budget_before_provider_dispatch(
    messages, redactor, reason
):
    client = Client(response("valid"))
    primary, ledger = wrapped(client, redactor=redactor)

    with pytest.raises(ValueError) as caught:
        primary.respond(messages)

    assert client.dispatches == 0
    assert ledger.entry.actual_tokens == 0
    assert ledger.entry.actual_cost == Decimal("0")
    assert ledger.entry.status == "settled"
    assert type(caught.value).__name__ == "PrimaryRequestNotDispatched"
    assert getattr(caught.value, "reason", None) == reason
    assert SYNTHETIC_MARKER not in str(caught.value)
    assert SYNTHETIC_MARKER not in repr(vars(caught.value))


UNSAFE_MODEL_NAMES = [
    pytest.param(None, id="missing"),
    pytest.param(42, id="integer"),
    pytest.param(["vendor/model"], id="list"),
    pytest.param({"model": "vendor/model"}, id="object"),
    pytest.param("", id="empty"),
    pytest.param(" vendor/model ", id="spaces"),
    pytest.param("vendor/model\nforged", id="newline"),
    pytest.param("vendor/model\x00forged", id="nul"),
    pytest.param("vendor/model\x7fforged", id="del"),
    pytest.param("vendor/model\u200bforged", id="format-control"),
    pytest.param("vendor/sk-" + "S" * 20, id="credential-pattern"),
    pytest.param("vendor/" + SYNTHETIC_MARKER, id="known-secret"),
    pytest.param("vendor/model?password=" + SYNTHETIC_MARKER, id="sensitive-marker"),
    pytest.param("vendor/" + "x" * 5000, id="unbounded"),
]


@pytest.mark.parametrize("model", UNSAFE_MODEL_NAMES)
def test_rejected_response_never_replaces_unsafe_actual_model_with_requested_model(model):
    primary, ledger = wrapped(Client(response(model=model)))

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert ledger.entry.actual_tokens == 14
    assert getattr(caught.value, "usage", None) == ModelUsage(11, 3, 14)
    assert getattr(caught.value, "model", None) is None
    assert SYNTHETIC_MARKER not in str(caught.value)
    assert SYNTHETIC_MARKER not in repr(vars(caught.value))


@pytest.mark.parametrize("model", UNSAFE_MODEL_NAMES)
def test_successful_response_uses_only_safe_actual_model_identifier(model):
    primary, ledger = wrapped(Client(response("valid", model=model)))

    turn = primary.respond([])

    assert ledger.entry.actual_tokens == 14
    assert turn.model is None


def test_safe_actual_model_identifier_is_preserved_exactly_on_success():
    primary, ledger = wrapped(Client(response("valid")))

    turn = primary.respond([])

    assert turn.model == ACTUAL_MODEL
    assert turn.model != primary.model.client.model_name
    assert ledger.entry.actual_cost == KNOWN_PRICE.cost(11, 3)


@pytest.mark.parametrize(
    "bad_usage",
    [
        ModelUsage(True, 3, 4),
        ModelUsage(11, False, 11),
        ModelUsage(11, 3, True),
        ModelUsage(-1, 3, 2),
        ModelUsage(1.0, 3, 4),
        ModelUsage("11", 3, 14),
        ModelUsage(11, 3, 99),
        dict(USAGE),
    ],
    ids=["bool-input", "bool-output", "bool-total", "negative", "float", "string", "sum", "dict"],
)
def test_trusted_rejection_constructor_does_not_trust_invalid_model_usage_dto(bad_usage):
    with pytest.raises(ValueError) as caught:
        PrimaryModel(Client(response())).respond([])
    assert getattr(caught.value, "usage", None) == ModelUsage(11, 3, 14)
    rejected_type = type(caught.value)
    assert rejected_type.__name__ == "PrimaryResponseRejected"
    valid = rejected_type(reason="tool_schema", usage=ModelUsage(11, 3, 14), model=ACTUAL_MODEL)
    assert valid.usage == ModelUsage(11, 3, 14)
    assert valid.model == ACTUAL_MODEL
    assert valid.reason == "tool_schema"

    try:
        constructed = rejected_type(reason="tool_schema", usage=bad_usage, model=ACTUAL_MODEL)
    except (ValueError, TypeError):
        return  # Strict constructor rejection is as safe as dropping the invalid DTO.
    assert getattr(constructed, "usage", None) is None


def test_trusted_rejection_constructor_does_not_copy_unlisted_reason():
    with pytest.raises(ValueError) as caught:
        PrimaryModel(Client(response())).respond([])
    assert getattr(caught.value, "usage", None) == ModelUsage(11, 3, 14)
    rejected_type = type(caught.value)
    valid = rejected_type(reason="tool_schema", usage=ModelUsage(11, 3, 14), model=ACTUAL_MODEL)
    assert valid.reason == "tool_schema"

    try:
        constructed = rejected_type(reason=SYNTHETIC_MARKER, usage=None, model=None)
    except (ValueError, TypeError) as rejected:
        assert SYNTHETIC_MARKER not in str(rejected)
        assert SYNTHETIC_MARKER not in repr(vars(rejected))
        return
    assert constructed.reason in {
        "tool_schema",
        "tool_identity",
        "invalid_tool_calls",
        "content_type",
        "usage_invalid",
        "unsafe_tool_proposal",
        "malformed_response",
        "empty_stream",
    }
    assert SYNTHETIC_MARKER not in str(constructed)
    assert SYNTHETIC_MARKER not in repr(vars(constructed))


def test_schema_error_does_not_copy_response_params_or_original_exception_text():
    def reject(name, params):
        raise ValueError(f"Injected schema payload: {params['path']}")

    client = Client(
        AIMessage(
            content=SYNTHETIC_MARKER,
            tool_calls=[{"name": "read_file", "args": {"path": SYNTHETIC_MARKER}, "id": "call"}],
            usage_metadata=dict(USAGE),
            response_metadata={"model_name": "vendor/model?password=" + SYNTHETIC_MARKER},
        )
    )
    ledger = Ledger()
    primary = BudgetedPrimary(
        PrimaryModel(client, tool_definitions=tool_schemas(), validator=reject),
        ledger,
        price=KNOWN_PRICE,
    )

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert ledger.entry.actual_tokens == 14
    assert getattr(caught.value, "reason", None) == "tool_schema"
    assert getattr(caught.value, "model", None) is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert SYNTHETIC_MARKER not in rendered
    assert SYNTHETIC_MARKER not in repr(caught.value.args)
    assert SYNTHETIC_MARKER not in repr(vars(caught.value))
    for field in ("response", "params", "tools", "proposals", "original"):
        assert getattr(caught.value, field, None) is None


@pytest.mark.parametrize("usage", INVALID_USAGE[2:])
def test_exhausted_stream_does_not_coerce_or_repair_invalid_chunk_usage(usage):
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": "read_file", "args": '{"path":"a.py"}', "id": "call", "index": 0}
        ],
    ).model_copy(update={"usage_metadata": usage})
    terminal = AIMessageChunk(content="", response_metadata={"model_name": ACTUAL_MODEL})
    client = Client(chunks=[chunk, terminal])
    primary, ledger = wrapped(client)

    with pytest.raises(ValueError) as caught:
        primary.respond([], stream=True)

    assert_unknown(ledger)
    assert type(caught.value).__name__ == "PrimaryResponseRejected"
    assert caught.value.reason == "usage_invalid"
    assert caught.value.usage is None
    assert client.dispatches == 1


def test_stream_preserves_original_model_when_repeated_in_multiple_chunks():
    chunks = [
        AIMessageChunk(content="hello", response_metadata={"model_name": ACTUAL_MODEL}),
        AIMessageChunk(
            content="",
            usage_metadata=dict(USAGE),
            response_metadata={"model_name": ACTUAL_MODEL},
        ),
    ]
    primary, ledger = wrapped(Client(chunks=chunks))

    turn = primary.respond([], stream=True)

    assert turn.model == ACTUAL_MODEL
    assert turn.usage == ModelUsage(11, 3, 14)
    assert ledger.entry.actual_tokens == 14


def test_non_stream_chunk_does_not_prove_complete_response_usage():
    client = Client(AIMessageChunk(content="partial", usage_metadata=dict(USAGE)))
    primary, ledger = wrapped(client)

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert_unknown(ledger)
    assert type(caught.value).__name__ != "PrimaryResponseRejected"
    assert client.dispatches == 1


def test_complete_message_with_malformed_tool_call_shape_keeps_valid_usage():
    malformed = response("valid").model_copy(update={"tool_calls": [None]})
    primary, ledger = wrapped(Client(malformed))

    with pytest.raises(ValueError) as caught:
        primary.respond([])

    assert ledger.entry.actual_tokens == 14
    assert ledger.entry.actual_cost == KNOWN_PRICE.cost(11, 3)
    assert type(caught.value).__name__ == "PrimaryResponseRejected"
    assert caught.value.reason == "malformed_response"
    assert caught.value.usage == ModelUsage(11, 3, 14)
