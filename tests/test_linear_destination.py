"""`LinearDestination`: the engine's field-keyed writes as Linear client calls.
All data here is invented."""

from pathlib import Path

import pytest
from gitops_engine.destination import Destination, DestinationError

from customer_registry_sync.deploy import restore
from customer_registry_sync.linear_client import LinearError, LiveCustomer
from customer_registry_sync.linear_destination import LinearDestination
from customer_registry_sync.linear_schema import LINEAR_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures"


class Client:
    def __init__(self, rows=()):
        self.rows = {r.id: r for r in rows}
        self.calls: list[tuple] = []

    def list_customers(self):
        self.calls.append(("list",))
        return list(self.rows.values())

    def status_ids(self):
        self.calls.append(("status_ids",))
        return {"Active": "s-act", "Churned": "s-chu"}

    def tier_ids(self):
        self.calls.append(("tier_ids",))
        return {"Gold": "t-gold"}

    def get_customer(self, cid):
        self.calls.append(("get", cid))
        return self.rows.get(cid)

    def create_customer(self, **kw):
        self.calls.append(("create", list(kw.items())))
        return LiveCustomer("lin_new", kw["name"], [], None, None, "", [], "v-new")

    def update_customer(self, cid, *, retire=False, **kw):
        self.calls.append(("update", cid, retire, list(kw.items())))
        return self.rows[cid]


def live(cid="lin_a", updated_at="u1"):
    return LiveCustomer(cid, "x", [], None, None, "Active", ["gadget"], updated_at)


def test_linear_destination_is_a_destination_and_linear_error_a_destination_error():
    assert isinstance(LinearDestination(Client()), Destination)
    assert issubclass(LinearError, DestinationError)


def test_reads_go_to_the_client():
    client = Client([live()])
    dest = LinearDestination(client)
    assert dest.list_live() == [live()] and dest.get("lin_a") == live()
    assert dest.get("lin_zz") is None


def test_preflight_status_first_then_tier():
    dest = LinearDestination(Client())
    with pytest.raises(ValueError) as e:
        dest.preflight([{"status": "Lost", "tier": "Bronze"}])
    assert str(e.value) == "unresolvable Linear status(es), nothing written: ['Lost']"
    with pytest.raises(ValueError) as e:
        dest.preflight([{"status": "Active", "tier": "Bronze"}, {"tier": None}])
    assert str(e.value) == "unresolvable Linear tier(s), nothing written: ['Bronze']"
    dest.preflight([{"tier": "Gold"}, {"name": "n"}])


def test_preflight_when_restoring_is_one_combined_message():
    dest = LinearDestination(Client())
    with pytest.raises(ValueError) as e:
        dest.preflight([{"status": "Lost"}, {"tier": "Bronze"}, {"tier": None}], restoring=True)
    assert str(e.value) == "unresolvable Linear status/tier, nothing restored: ['Bronze', 'Lost']"


def test_create_maps_status_and_tier_in_v010_kwarg_order():
    client = Client()
    dest = LinearDestination(client)
    dest.preflight([])
    dest.create(
        {
            "name": "N",
            "status": "Active",
            "external_ids": ["n"],
            "domains": ["n.example.test"],
            "revenue": 7,
            "tier": "Gold",
        }
    )
    assert client.calls[-1] == (
        "create",
        [
            ("name", "N"),
            ("domains", ["n.example.test"]),
            ("external_ids", ["n"]),
            ("status_id", "s-act"),
            ("tier_id", "t-gold"),
            ("revenue", 7),
        ],
    )


def test_update_maps_fields_in_value_order_and_clears_tier_with_none():
    client = Client([live()])
    dest = LinearDestination(client)
    dest.preflight([])
    dest.update("lin_a", {"status": "Churned", "domains": ["a"], "tier": None}, retire=True)
    assert client.calls[-1] == (
        "update",
        "lin_a",
        True,
        [("status_id", "s-chu"), ("domains", ["a"]), ("tier_id", None)],
    )


def test_ids_resolve_without_an_explicit_preflight():
    client = Client([live()])
    LinearDestination(client).update("lin_a", {"tier": "Gold"}, retire=False)
    assert client.calls == [
        ("status_ids",),
        ("tier_ids",),
        ("update", "lin_a", False, [("tier_id", "t-gold")]),
    ]


def test_restore_v010_snapshot():
    """A snapshot written by v0.1.0's write_snapshot restores with exactly the
    `update_customer` calls v0.1.0 made for it."""
    client = Client(
        [live("lin_a", "2026-09-03T12:00:05.000Z"), live("lin_b", "2026-09-02T11:00:00.000Z")]
    )
    res = restore(FIXTURES / "snapshot-v010.yaml", client)
    assert (res.restored, res.skipped, res.failure) == (["lin_a", "lin_b"], [], None)
    assert client.calls == [
        ("status_ids",),
        ("tier_ids",),
        ("get", "lin_a"),
        (
            "update",
            "lin_a",
            False,
            [
                ("name", "Gadget Works"),
                ("domains", ["gadget.example.test", "Gadget.example.org"]),
                ("external_ids", ["gadget"]),
                ("revenue", 1200),
                ("tier_id", "t-gold"),
                ("status_id", "s-act"),
            ],
        ),
        ("get", "lin_b"),
        (
            "update",
            "lin_b",
            True,
            [
                ("name", "Sprocket Ltd"),
                ("domains", []),
                ("external_ids", ["sprocket", "sprk"]),
                ("revenue", None),
                ("tier_id", None),
            ],
        ),
    ]


def test_snapshot_row_keeps_v010_key_order():
    lv = LiveCustomer("lin_a", "N", ["d"], 1, "Gold", "Active", ["n"], "u1")
    assert list(LINEAR_SCHEMA.snapshot_row(lv)) == [
        "linear_id",
        "name",
        "domains",
        "revenue",
        "tier",
        "status",
        "external_ids",
        "updated_at",
    ]
