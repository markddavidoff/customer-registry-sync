"""Tests for the gitops-engine adapter that drives Linear Customers.

These exercise the adapter contract directly (matching order, preflight) and the
`deploy` wrappers with the engine in the loop (plan-time and write-time aborts).
All data here is invented.
"""

import pytest

from customer_registry_sync.deploy import apply_deploy, live_to_linear, plan_deploy
from customer_registry_sync.linear_adapter import LinearCustomersAdapter
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of


def desired(**kw) -> LinearCustomer:
    base = dict(
        name="Helios Freight",
        domains=["helios.example.test"],
        revenue=1000,
        tier="Tier 1",
        status="Active",
        external_ids=["helios-freight"],
    )
    base.update(kw)
    return LinearCustomer(**base)


def livec(**kw) -> LiveCustomer:
    base = dict(
        id="lin-1",
        name="Helios Freight",
        domains=["helios.example.test"],
        revenue=1000,
        tier="Tier 1",
        status="Active",
        external_ids=["helios-freight"],
        updated_at="2026-10-07T00:00:00.000Z",
    )
    base.update(kw)
    return LiveCustomer(**base)


class FakeClient:
    """Mirrors the fake transport in test_deploy.py: records create/update calls so
    a test can assert nothing was written."""

    def __init__(self, live_now, status_map=None):
        self._live = {lv.id: lv for lv in live_now}
        self._status = status_map or {"Active": "act", "Prospect": "pro", "Churned": "chu"}
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def status_ids(self):
        return dict(self._status)

    def tier_ids(self):
        return {"Tier 1": "t1", "Tier 2": "t2"}

    def get_customer(self, cid):
        return self._live.get(cid)

    def create_customer(self, **kw):
        self.created.append(kw)
        return LiveCustomer(
            id="new-id",
            name=kw["name"],
            domains=kw["domains"],
            revenue=kw.get("revenue"),
            tier=None,
            status=kw.get("status", "Active"),
            external_ids=kw["external_ids"],
            updated_at="2026-11-01T00:00:00.000Z",
        )

    def update_customer(self, cid, **kw):
        self.updated.append((cid, kw))
        return LiveCustomer(
            id=cid,
            name=kw.get("name", "?"),
            domains=kw.get("domains", []),
            revenue=kw.get("revenue"),
            tier=None,
            status=kw.get("status", "Active"),
            external_ids=kw.get("external_ids", []),
            updated_at="2026-11-02T00:00:00.000Z",
        )


def test_adapter_matches_by_external_id_then_domain_then_name_then_alias():
    # curated alias: our dataset name -> an existing Linear customer name (no xid/domain/name overlap)
    aliases = {"Wayne Technologies": "Gotham Industries"}
    adapter = LinearCustomersAdapter(client=None, aliases=aliases)

    by_xid = livec(
        id="x1", name="Totally Other", domains=["other.example.test"], external_ids=["xenon-labs"]
    )
    by_dom = livec(id="x2", name="Also Other", domains=["orbit.example.test"], external_ids=[])
    by_name = livec(id="x3", name="Helios Freight [Active]", domains=[], external_ids=[])
    by_alias = livec(id="x4", name="Gotham Industries", domains=[], external_ids=[])

    # external id wins even when domain and name would match other rows
    d_xid = desired(
        name="Helios Freight [Active]", domains=["orbit.example.test"], external_ids=["xenon-labs"]
    )
    assert adapter.match(d_xid, [by_xid, by_dom, by_name]) is by_xid

    # domain is next when the external id does not resolve
    d_dom = desired(name="No Name Match", domains=["orbit.example.test"], external_ids=["no-such"])
    assert adapter.match(d_dom, [by_dom, by_name]) is by_dom

    # normalized name is next (status suffix ignored on both sides)
    d_name = desired(name="Helios Freight [Prospect]", domains=[], external_ids=[])
    assert adapter.match(d_name, [by_name]) is by_name

    # curated alias is the last resort
    d_alias = desired(name="Wayne Technologies", domains=[], external_ids=[])
    assert adapter.match(d_alias, [by_alias]) is by_alias

    # nothing matches -> None (a create)
    assert adapter.match(desired(name="Ghost Co", domains=[], external_ids=[]), []) is None


def test_preflight_raises_for_unresolvable_status_before_any_write():
    ok = desired(
        name="Solvable",
        domains=["solv.example.test"],
        external_ids=["solvable"],
        status="Active",
        linear_synced=None,
    )
    bad = desired(
        name="Unsolvable",
        domains=["uns.example.test"],
        external_ids=["unsolvable"],
        status="Churned",
        linear_synced=None,
    )
    plan = plan_deploy([ok, bad], live=[])
    assert len(plan.creates) == 2

    client = FakeClient(live_now=[], status_map={"Active": "act", "Prospect": "pro"})  # no Churned
    with pytest.raises(ValueError, match="unresolvable Linear status"):
        apply_deploy(plan, client)
    # fail-fast: nothing written even though "Solvable" was resolvable
    assert client.created == [] and client.updated == []


def test_abort_when_state_matched_only_via_live_id_and_both_moved():
    # The state row carries NO external id, so it is located by the live id, not the
    # desired external id. version AND content moved -> plan-time abort.
    recorded = livec(
        id="lin-9",
        name="Zephyr Logistics",
        domains=["zephyr.example.test"],
        external_ids=[],
        revenue=1,
        updated_at="2026-01-01T00:00:00.000Z",
    )
    st = live_to_linear(recorded)
    st.external_ids = []  # nothing for state_by_xid to key on
    st.linear_synced = LinearSynced("lin-9", recorded.updated_at, content_hash_of(st))

    live_now = livec(
        id="lin-9",
        name="Zephyr Logistics",
        domains=["zephyr.example.test"],
        external_ids=["zephyr-logistics"],
        revenue=999,
        updated_at="2026-09-09T00:00:00.000Z",
    )
    d = desired(
        name="Zephyr Logistics",
        domains=["zephyr.example.test"],
        external_ids=["zephyr-logistics"],
        revenue=5,
    )

    plan = plan_deploy([d], [live_now], state=[st])
    assert len(plan.aborts) == 1 and not plan.updates


def test_first_link_row_aborted_at_write_time_when_live_changed():
    # No mark (first link). Plan says update; between plan and write the live row
    # moved -> check-and-set aborts rather than clobber.
    first_link = desired(
        name="Nimbus Freight",
        domains=["nimbus.example.test"],
        external_ids=["nimbus-freight"],
        revenue=999,
        linear_synced=None,
    )
    plan_live = livec(
        id="lin-5",
        name="Nimbus Freight",
        domains=["nimbus.example.test"],
        external_ids=["nimbus-freight"],
        revenue=1,
        updated_at="2026-02-02T00:00:00.000Z",
    )
    plan = plan_deploy([first_link], live=[plan_live])
    assert len(plan.updates) == 1

    # get_customer now reports a DIFFERENT updatedAt -> write-time drift
    client = FakeClient(
        live_now=[
            livec(
                id="lin-5",
                name="Nimbus Freight",
                domains=["nimbus.example.test"],
                external_ids=["nimbus-freight"],
                updated_at="2026-12-12T00:00:00.000Z",
            )
        ]
    )
    apply_deploy(plan, client)
    assert client.updated == []  # must not clobber the concurrently-edited row
