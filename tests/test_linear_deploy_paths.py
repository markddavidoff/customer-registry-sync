"""Drift through `check.detect_drift`, the one pairing (`identity.pair_rows`) and the
`deploy` planner and apply on the engine (plan-time and write-time aborts, the
never-null write path). All data here is invented.
"""

import pytest

from customer_registry_sync.check import detect_drift
from customer_registry_sync.deploy import apply_deploy, plan_deploy
from customer_registry_sync.identity import (
    IDENTITIES_FILE,
    IdentityTable,
    load_identities,
    pair_rows,
)
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import (
    LINEAR_SCHEMA,
    LinearCustomer,
    LinearSynced,
    content_hash_of,
)


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

    def update_customer(self, cid, *, retire=False, **kw):
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


def _table(tmp_path, *identities):
    """An identity table written by the real producer; each identity is
    (canonical id, names, domains, sources)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        assert store.add_identity(cid, names=names, domains=domains, sources=sources) == cid
    store.save()
    return load_identities(tmp_path / IDENTITIES_FILE)


def _match(d, live, table=None):
    """The live customer `pair_rows` pairs with `d`: the one pairing check and deploy use."""
    [(_, lv)] = pair_rows([d], live, table or IdentityTable()).pairs
    return lv


def test_pairing_matches_through_identity_resolve(tmp_path):
    by_xid = livec(
        id="x1", name="Totally Other", domains=["other.example.test"], external_ids=["xenon-labs"]
    )
    by_dom = livec(id="x2", name="Also Other", domains=["orbit.example.test"], external_ids=[])
    by_name = livec(id="x3", name="Helios Freight [Active]", domains=[], external_ids=[])

    # the customer stamped with the row's external id wins over a domain or name match
    d_xid = desired(
        name="Helios Freight [Active]", domains=["orbit.example.test"], external_ids=["xenon-labs"]
    )
    assert _match(d_xid, [by_xid]) is by_xid

    # domain when no customer carries the external id
    d_dom = desired(name="No Name Match", domains=["orbit.example.test"], external_ids=["no-such"])
    assert _match(d_dom, [by_dom, by_name]) is by_dom

    # name next (status suffix ignored on both sides)
    d_name = desired(name="Helios Freight [Prospect]", domains=[], external_ids=[])
    assert _match(d_name, [by_name]) is by_name

    # nothing matches -> None (a create)
    assert _match(desired(name="Ghost Co", domains=[], external_ids=[]), []) is None

    # a Linear name recorded on the identity links rows no key would match (no aliases)
    by_identity_name = livec(id="x4", name="Gotham Industries", domains=[], external_ids=[])
    d_ident = desired(name="Wayne Technologies", domains=[], external_ids=["wayne"])
    assert _match(d_ident, [by_identity_name]) is None
    table = _table(tmp_path, ("wayne", ["Wayne Technologies", "Gotham Industries"], [], {}))
    assert _match(d_ident, [by_identity_name], table) is by_identity_name


def test_pairing_on_a_duplicate_group_uses_pick_primary(tmp_path):
    d = desired(name="Helios Freight", domains=["helios.example.test"], external_ids=["helios"])
    a = livec(id="lin-a", name="Helios Freight", external_ids=[])
    b = livec(id="lin-b", name="Helios Freight Inc", external_ids=[])
    # neither carries the canonical id and none is recorded: no pair, never API order
    pairing = pair_rows([d], [a, b], IdentityTable())
    assert pairing.pairs == [] and pairing.errors
    table = _table(
        tmp_path, ("helios", ["Helios Freight"], ["helios.example.test"], {"linear": "lin-b"})
    )
    assert _match(d, [a, b], table) is b
    stamped = livec(id="lin-c", name="Helios Freight Inc", external_ids=["helios"])
    assert _match(d, [a, b, stamped], table) is stamped


def test_drift_hashes_an_empty_live_status_as_the_mirror_records_it():
    lv = livec(status="")
    st = LINEAR_SCHEMA.mirror_row(lv)
    assert st.status is None
    st.linear_synced = LinearSynced(lv.id, lv.updated_at, content_hash_of(st))
    assert not detect_drift([st], [lv]).any()


def test_tierless_desired_rows_apply_without_sending_nulls(tmp_path, recording_linear):
    """The client refuses null/[] unless retire=True; a row with no tier (and no
    domains) must create and update cleanly through the local apply."""
    new = desired(name="Tierless Co", domains=[], tier=None, revenue=None, external_ids=["tl"])
    lv = recording_linear.node(
        "lin-9",
        "Helios Freight",
        domains=["helios.example.test"],
        revenue=1,
        xids=["helios-freight"],
    )
    upd = desired(tier=None, revenue=5)
    plan = plan_deploy([new, upd], [lv])
    assert len(plan.creates) == 1 and len(plan.updates) == 1
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert res.failure is None and not plan.aborts
    [create] = [c for c in client.calls if c[0] == "create"]
    assert "tier_id" not in create[1] and "domains" not in create[1]
    [(_, _, fields, retire, _)] = client.updates()
    assert (fields, retire) == ({"revenue": 5}, False)


def test_preflight_raises_for_unresolvable_status_before_any_write(tmp_path):
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
        apply_deploy(plan, client, snapshot_dir=tmp_path)
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
    st = LINEAR_SCHEMA.mirror_row(recorded)
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


def test_first_link_row_aborted_at_write_time_when_live_changed(tmp_path):
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
    apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert client.updated == []  # must not clobber the concurrently-edited row
