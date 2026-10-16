"""Real PostgreSQL lifecycle and receipt reconciliation, in disposable databases."""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def tickets(database):
    from evals.integration.business_support import FixtureTickets

    store = FixtureTickets(database)
    assert store.execute("seed", {}, "seed")["ok"]
    return store


def directory(tickets):
    return tickets.execute("directory", {}, "directory")["data"]


def create(tickets, key=None):
    category = directory(tickets)["categories"][0]["id"]
    result = tickets.execute(
        "create", {"title": "VPN unavailable", "category_id": category}, key or str(uuid4())
    )
    assert result["ok"], result
    return result["data"]["ticket"]


def update(tickets, ticket, command, **params):
    result = tickets.execute(
        command,
        {"ticket_id": ticket["id"], "expected_revision": ticket["revision"], **params},
        str(uuid4()),
    )
    assert result["ok"], result
    return result["data"]["ticket"]


def test_complete_lifecycle_preserves_comments_history_and_reopen(tickets):
    ticket = create(tickets)
    assert ticket["status"] == "open"
    ticket = update(
        tickets,
        ticket,
        "assign",
        person_id=next(p["id"] for p in directory(tickets)["people"] if p["name"] == "Zhang Wei"),
    )
    assert ticket["status"] == "assigned"
    ticket = update(tickets, ticket, "start")
    assert ticket["status"] == "in_progress"
    ticket = update(tickets, ticket, "comment", body="Checked network")
    ticket = update(tickets, ticket, "set_priority", priority="urgent")
    ticket = update(tickets, ticket, "resolve", resolution="User reports VPN reconnects")
    assert ticket["status"] == "resolved"
    ticket = update(tickets, ticket, "close")
    assert ticket["status"] == "closed"
    ticket = update(tickets, ticket, "reopen")
    assert ticket["status"] == "open" and ticket["assignee_id"] is None
    fetched = tickets.execute("get", {"ticket_id": ticket["id"]}, "get")["data"]
    assert len(fetched["history"]) == 8
    assert fetched["history"][3]["details"]["body"] == "Checked network"


def test_illegal_transition_and_stale_revision_do_not_mutate(tickets):
    ticket = create(tickets)
    illegal = tickets.execute(
        "close", {"ticket_id": ticket["id"], "expected_revision": 1}, "illegal-close"
    )
    assert illegal["error"]["code"] == "invalid_transition"
    updated = update(tickets, ticket, "comment", body="An observation")
    stale = tickets.execute(
        "comment",
        {"ticket_id": ticket["id"], "expected_revision": 1, "body": "Stale observation"},
        "stale",
    )
    assert stale["error"]["code"] == "revision_conflict"
    assert (
        tickets.execute("get", {"ticket_id": ticket["id"]}, "read")["data"]["ticket"]["revision"]
        == updated["revision"]
    )


def test_duplicate_directory_names_keep_ids_and_teams(tickets):
    for name in ("Zhang Wei", "张伟"):
        candidates = tickets.execute("directory", {"kind": "people", "query": name}, name)["data"][
            "people"
        ]
        assert len(candidates) == 2
        assert len({person["id"] for person in candidates}) == 2
        assert len({person["team"] for person in candidates}) == 2


def test_idempotency_collision_and_lost_response_receipt(tickets):
    category = directory(tickets)["categories"][0]["id"]
    params = {"title": "Printer unavailable", "category_id": category}
    first = tickets.execute("create", params, "lost-response")
    assert tickets.execute("create", params, "lost-response") == first
    conflict = tickets.execute("create", {**params, "title": "Different request"}, "lost-response")
    assert conflict["error"]["code"] == "operation_conflict"
    receipt = tickets.execute(
        "receipt", {"operation_id": first["data"]["receipt"]["operation_id"]}, "lookup"
    )["data"]["receipt"]
    assert receipt["resource_id"] == first["data"]["ticket"]["id"]
    assert receipt["result"]["ticket"] == first["data"]["ticket"]
    assert len(tickets.execute("list", {}, "list")["data"]["tickets"]) == 1


def test_registry_replacement_and_disable_block_frozen_contract(database):
    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest

    registry = PluginRegistry(database)
    source = builtin_manifest()
    scope = registry.register(source)
    spec = registry.get(scope["id"])
    assert len(registry.list()) == 1
    source["version"] = "1.0.1"
    with pytest.raises(ValueError, match="version 2.0.0"):
        registry.register(source)
    source = builtin_manifest()
    source["commands"]["create"]["description"] += " (reviewed contract update)"
    updated = registry.register(source)
    assert updated["id"] == scope["id"] and updated["revision"] == 2
    with pytest.raises(ValueError, match="changed"):
        registry.get(scope["id"], expected_digest=spec.digest)
    registry.disable(scope["id"])
    with pytest.raises(ValueError, match="disabled"):
        registry.get(scope["id"])


def test_failed_write_leaves_no_history_or_receipt(tickets):
    from decagent.business.tickets.store import TicketStore

    category = str(uuid4())
    result = tickets.execute(
        "create", {"title": "Invalid category", "category_id": category}, "rolled-back"
    )
    assert result["error"]["code"] == "invalid_reference"
    assert (
        tickets.execute("receipt", {"operation_id": "rolled-back"}, "find")["data"]["receipt"]
        is None
    )
    assert not tickets.execute("list", {}, "list")["data"]["tickets"]
    assert TicketStore(tickets.database.url).execute("directory", {}, "restart")["data"]["people"]
