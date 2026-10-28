from dataclasses import replace

from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetRecord
from customer_registry_sync.deploy import (
    apply_deploy,
    plan_deploy,
)
from gitops_engine.fieldplan import FieldChange
from gitops_engine.pairing import pick_primary
from customer_registry_sync.generate import generate_linear, merge_datasets
from customer_registry_sync.identity import (
    IDENTITIES_FILE,
    IDENTITY_SPEC,
    IdentityTable,
    load_identities,
    pair_rows,
)
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_client import LiveCustomer, parse_customer_node
from customer_registry_sync.linear_schema import (
    LINEAR_SCHEMA,
    LinearCustomer,
    LinearSynced,
    content_hash_of,
)
from customer_registry_sync.schema import CustomerRecord


def desired(**kw) -> LinearCustomer:
    base = dict(
        name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        revenue=250000,
        tier="Tier 1",
        status="Active",
        external_ids=["acmerobotics"],
    )
    base.update(kw)
    return LinearCustomer(**base)


def livec(**kw) -> LiveCustomer:
    base = dict(
        id="lin-1",
        name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        revenue=250000,
        tier="Tier 1",
        status="Active",
        external_ids=["acmerobotics"],
        updated_at="2026-10-07T00:00:00.000Z",
    )
    base.update(kw)
    return LiveCustomer(**base)


def test_no_live_match_is_a_create():
    plan = plan_deploy([desired(external_ids=["new"], name="New")], live=[])
    assert [c.name for c in plan.creates] == ["New"]
    assert not plan.updates and not plan.aborts and not plan.orphans


def test_matching_prestate_and_changed_content_is_an_update():
    # live matches by external id; pre-state updatedAt matches live; content differs (revenue)
    d = desired(
        revenue=999999,
        linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "irrelevant"),
    )
    plan = plan_deploy([d], live=[livec(revenue=250000)])
    assert len(plan.updates) == 1 and not plan.aborts and not plan.creates


def test_drift_is_an_abort():
    # pre-state (from the state mirror) updatedAt does NOT match live's current
    # updatedAt and the live content differs from the recorded hash -> drift
    d = desired(revenue=999999)
    st = desired(linear_synced=LinearSynced("lin-1", "2026-01-01T00:00:00.000Z", "x"))
    plan = plan_deploy([d], live=[livec(updated_at="2026-10-07T00:00:00.000Z")], state=[st])
    assert len(plan.aborts) == 1 and not plan.updates


def test_unchanged_content_is_not_an_update():
    d = desired(linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "x"))
    plan = plan_deploy([d], live=[livec()])  # identical deployable content
    assert d in plan.unchanged and not plan.updates


def test_identity_name_links_desired_to_live_instead_of_dup_create(tmp_path):
    # our dataset name/domain can't match the live row by domain or name
    d = desired(
        name="Stark Fabrication LLC (The Forge)",
        domains=[],
        external_ids=["stark-fabrication-llc-the-forge"],
        linear_synced=None,
    )
    live = [
        livec(
            id="lin-forge", name="The Forge", domains=["starkforge.example.test"], external_ids=[]
        )
    ]
    # without the Linear name on the identity: a duplicate create + a false orphan
    plan0 = plan_deploy([d], live, IdentityTable())
    assert len(plan0.creates) == 1 and len(plan0.orphans) == 1
    # with it: an update, no create, no orphan
    table = _table(
        tmp_path,
        (
            "stark-fabrication-llc-the-forge",
            ["Stark Fabrication LLC (The Forge)", "The Forge"],
            [],
            {},
        ),
    )
    plan = plan_deploy([d], live, table)
    assert len(plan.updates) == 1 and not plan.creates and not plan.orphans


def test_live_with_no_desired_match_is_orphan_reported():
    plan = plan_deploy([], live=[livec(id="orph", name="Orphan Co", domains=[], external_ids=[])])
    assert [o.name for o in plan.orphans] == ["Orphan Co"]


class FakeClient:
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
        # apply_deploy sends only the planned fields: apply them to the current row
        self.updated.append((cid, kw))
        cur = self._live[cid]
        sent = {k: v for k, v in kw.items() if k in ("name", "domains", "revenue", "external_ids")}
        return replace(cur, **sent, updated_at="2026-11-02T00:00:00.000Z")


def test_apply_issues_create_and_update_only_and_writes_back_linear_synced(tmp_path):
    create = desired(name="New", domains=["new.com"], external_ids=["new"], linear_synced=None)
    update = desired(
        revenue=999999, linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "x")
    )
    live = [livec(revenue=250000)]  # matches `update` by external id, pre-state matches
    plan = plan_deploy([create, update], live=live)
    assert len(plan.creates) == 1 and len(plan.updates) == 1

    client = FakeClient(live_now=live)
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert len(client.created) == 1 and len(client.updated) == 1
    assert res.failure is None and res.created == ["new-id"]
    # linear_synced written back from the write result
    by_name = {r.name: r for r in res.persisted}
    assert by_name["New"].linear_synced.linear_id == "new-id"
    assert by_name["Acme Robotics"].linear_synced.updated_at == "2026-11-02T00:00:00.000Z"
    # content_hash recomputed on the persisted record
    assert by_name["Acme Robotics"].linear_synced.content_hash == content_hash_of(
        by_name["Acme Robotics"]
    )


def test_apply_aborts_on_write_time_drift(tmp_path):
    # plan says update, but between plan and apply the live row drifted
    update = desired(
        revenue=999999, linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "x")
    )
    plan = plan_deploy([update], live=[livec(revenue=250000)])
    assert len(plan.updates) == 1
    # client's get_customer now reports a DIFFERENT updatedAt -> write-time drift
    client = FakeClient(live_now=[livec(updated_at="2026-12-31T00:00:00.000Z")])
    apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert not client.updated  # nothing written


def test_apply_aborts_first_link_row_on_write_time_drift(tmp_path):
    # first-link row (linear_synced is None) must ALSO be guarded by check-and-set
    first_link = desired(revenue=999999, linear_synced=None)
    plan = plan_deploy([first_link], live=[livec(revenue=250000)])  # match by external id -> update
    assert len(plan.updates) == 1
    # live drifted between plan (updatedAt 2026-10-07) and apply (2026-12-31)
    client = FakeClient(live_now=[livec(updated_at="2026-12-31T00:00:00.000Z")])
    apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert not client.updated  # must NOT clobber the concurrently-edited row


def test_apply_preflight_writes_nothing_on_unresolvable_status(tmp_path):
    ok = desired(
        name="A", domains=["a.com"], external_ids=["a"], status="Active", linear_synced=None
    )
    bad = desired(
        name="C", domains=["c.com"], external_ids=["c"], status="Churned", linear_synced=None
    )
    plan = plan_deploy([ok, bad], live=[])
    assert len(plan.creates) == 2
    import pytest

    client = FakeClient(live_now=[], status_map={"Active": "act", "Prospect": "pro"})  # no Churned
    with pytest.raises(ValueError, match="status"):
        apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert client.created == []  # fail-fast: nothing written even though "A" was resolvable


def _live(lid, name, updated, revenue=1, xids=("acme",)):
    return LiveCustomer(
        id=lid,
        name=name,
        domains=["acme.example.test"],
        revenue=revenue,
        tier=None,
        status="Active",
        external_ids=list(xids),
        updated_at=updated,
    )


def _state_from(lv):
    s = LINEAR_SCHEMA.mirror_row(lv)
    s.linear_synced = LinearSynced(lv.id, lv.updated_at, content_hash_of(s))
    return s


def test_plan_aborts_when_state_updated_at_differs_and_content_changed():
    recorded = _live("lin_1", "Acme", "2026-10-01T00:00:00.000Z", revenue=1)
    live_now = _live(
        "lin_1", "Acme", "2026-10-02T00:00:00.000Z", revenue=99
    )  # someone edited revenue
    desired = LinearCustomer(
        name="Acme",
        domains=["acme.example.test"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    plan = plan_deploy([desired], [live_now], state=[_state_from(recorded)])
    assert len(plan.aborts) == 1 and not plan.updates


def test_plan_metadata_only_bump_is_not_drift():
    recorded = _live("lin_1", "Acme", "2026-10-01T00:00:00.000Z", revenue=1)
    live_now = _live(
        "lin_1", "Acme", "2026-10-02T00:00:00.000Z", revenue=1
    )  # updatedAt moved, content same
    desired = LinearCustomer(
        name="Acme",
        domains=["acme.example.test"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    plan = plan_deploy([desired], [live_now], state=[_state_from(recorded)])
    assert not plan.aborts and len(plan.updates) == 1


def test_plan_without_state_never_aborts_at_plan_time():
    live_now = _live("lin_1", "Acme", "2026-10-02T00:00:00.000Z", revenue=1)
    desired = LinearCustomer(
        name="Acme",
        domains=["acme.example.test"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    plan = plan_deploy([desired], [live_now])  # first link: nothing recorded yet
    assert not plan.aborts and len(plan.updates) == 1


def test_apply_never_deletes_orphans(tmp_path):
    from customer_registry_sync.deploy import DeployPlan, apply_deploy

    deleted: list[str] = []

    class C:
        def status_ids(self):
            return {"Active": "s"}

        def tier_ids(self):
            return {}

        def delete_customer(self, cid):
            deleted.append(cid)

    plan = DeployPlan(orphans=[_live("lin_orphan", "Orphan", "2026-10-01T00:00:00.000Z")])
    res = apply_deploy(plan, C(), snapshot_dir=tmp_path)
    assert res.persisted == [] and res.created == [] and deleted == []


def test_persisted_state_hashes_what_linear_returned_not_desired(tmp_path):
    """Seam: the state mirror must come from the row Linear RETURNED, because
    check/plan_deploy/sync-inbound hash the live row, not the desired one."""
    want = LinearCustomer(
        name="Acme",
        domains=["acme.example.test", "NEW.com"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    live0 = _live("lin_1", "Acme", "2026-10-01T00:00:00.000Z", revenue=1)
    plan = plan_deploy([want], [live0])

    def normalizing(returned_revenue):
        class Normalizing:
            def status_ids(self):
                return {"Active": "s"}

            def tier_ids(self):
                return {}

            def get_customer(self, cid):
                return live0

            def update_customer(self, cid, *, retire=False, **f):
                # Linear "normalizes": lowercases domains (and may round revenue)
                assert f == {"domains": ["acme.example.test", "NEW.com"], "revenue": 5}
                return LiveCustomer(
                    id="lin_1",
                    name="Acme",
                    domains=["acme.example.test", "new.com"],
                    revenue=returned_revenue,
                    tier=None,
                    status="Active",
                    external_ids=["acme"],
                    updated_at="2026-10-03T00:00:00.000Z",
                )

        return Normalizing()

    res = apply_deploy(plan, normalizing(5), snapshot_dir=tmp_path / "a")
    [st] = res.persisted
    assert res.failure is None  # a re-cased domain is not a mismatch
    assert st.domains == ["acme.example.test", "new.com"]
    assert st.linear_synced.updated_at == "2026-10-03T00:00:00.000Z"
    assert st.linear_synced.content_hash == content_hash_of(st)
    assert st.linear_synced.content_hash != content_hash_of(want)

    # a value Linear changed (rounded revenue) still persists what Linear returned,
    # and post-write verification fails the run
    res = apply_deploy(plan, normalizing(10), snapshot_dir=tmp_path / "b")
    [st] = res.persisted
    assert st.revenue == 10 and res.failure == "Acme: revenue expected 5 got 10"


def test_plan_records_changes_and_target():
    from customer_registry_sync.linear_client import parse_customer_node

    lv = parse_customer_node(
        {
            "id": "lin-1",
            "name": "Acme Robotics",
            "domains": ["acmerobotics.example.org"],
            "revenue": 300000,
            "externalIds": ["acmerobotics"],
            "updatedAt": "2026-10-07T00:00:00.000Z",
            "status": {"id": "s", "name": "Active"},
            "tier": None,
        }
    )
    d = desired(domains=["acmerobotics.example.test"], revenue=250000, tier=None)
    plan = plan_deploy([d], [lv])
    [(target, plv)] = plan.updates
    assert plv is lv
    assert (
        sorted(target.domains) == ["acmerobotics.example.org", "acmerobotics.example.test"]
        and target.revenue == 250000
    )
    assert [(c.field, c.kind) for c in plan.changes["lin-1"]] == [
        ("domains", "add"),
        ("revenue", "change"),
    ]
    assert plan.unacked == [] and not plan.unchanged


def test_plan_unchanged_when_only_live_extras():
    # live carries a domain and a tier the registry does not: kept, not an update
    d = desired(domains=["acmerobotics.example.test"], tier=None)
    lv = livec(domains=["acmerobotics.example.test", "acmerobotics.example.org"], tier="Tier 2")
    plan = plan_deploy([d], [lv])
    assert not plan.updates and len(plan.unchanged) == 1
    [target] = plan.unchanged
    assert (
        sorted(target.domains) == ["acmerobotics.example.org", "acmerobotics.example.test"]
        and target.tier == "Tier 2"
    )
    assert plan.changes["lin-1"] == []


def test_plan_retire_marks_destructive_as_acknowledged():
    d = desired()
    lv = livec(domains=["acmerobotics.example.test", "old.io"])
    plan = plan_deploy([d], [lv], retire={"acmerobotics": {"domains": ["old.io"]}})
    [(target, _)] = plan.updates
    assert target.domains == ["acmerobotics.example.test"]
    [c] = plan.changes["lin-1"]
    assert (c.kind, c.retire) == ("remove", True) and plan.unacked == []


def test_name_fallback_ignores_status_suffix_on_both_sides():
    d = desired(
        name="Gringotts Advisory [Prospect]", domains=[], external_ids=[], linear_synced=None
    )
    for live_name in (
        "Gringotts Advisory [Active]",
        "Gringotts Advisory",
        "Gringotts Advisory [Prospect]",
        "Gringotts Advisory [Churned]",
    ):
        lv = livec(id="x", name=live_name, domains=[], external_ids=[])
        assert pair_rows([d], [lv], IdentityTable()).pairs == [(d, lv)]
    plain = desired(name="Gringotts Advisory", domains=[], external_ids=[], linear_synced=None)
    lv = livec(id="y", name="Gringotts Advisory [Active]", domains=[], external_ids=[])
    assert pair_rows([plain], [lv], IdentityTable()).pairs == [(plain, lv)]


def test_name_fallback_does_not_match_different_names_or_non_status_brackets():
    d = desired(name="Acme [Active]", domains=[], external_ids=[], linear_synced=None)
    for lv in (
        livec(id="a", name="Acme [Beta]", domains=[], external_ids=[]),
        livec(id="b", name="Other [Active]", domains=[], external_ids=[]),
    ):
        got = pair_rows([d], [lv], IdentityTable())
        assert got.pairs == [(d, None)] and got.unmatched == [lv]


def test_identity_name_still_links_when_desired_name_is_suffixed(tmp_path):
    d = desired(
        name="Stark Fabrication LLC (The Forge) [Active]",
        domains=[],
        external_ids=["stark-forge"],
        linear_synced=None,
    )
    table = _table(
        tmp_path, ("stark-forge", ["Stark Fabrication LLC (The Forge)", "The Forge"], [], {})
    )
    live = [
        livec(
            id="lin-forge", name="The Forge", domains=["starkforge.example.test"], external_ids=[]
        )
    ]
    plan = plan_deploy([d], live, table)
    assert len(plan.updates) == 1 and not plan.creates and not plan.orphans
    live2 = [
        livec(
            id="lin-forge",
            name="The Forge [Active]",
            domains=["starkforge.example.test"],
            external_ids=[],
        )
    ]
    plan2 = plan_deploy([d], live2, table)
    assert len(plan2.updates) == 1 and not plan2.creates and not plan2.orphans


# ---- snapshot before apply, only planned fields, post-write verification ----


def _robotics(rl, **kw):
    base = {
        "domains": ["acmerobotics.example.test"],
        "revenue": 250000,
        "tier": "Tier 1",
        "xids": ["acmerobotics"],
        "updated": "2026-10-07T00:00:00.000Z",
    }
    base.update(kw)
    return rl.node("lin-1", "Acme Robotics", **base)


def test_snapshot_written_before_first_mutation(tmp_path, recording_linear):
    from customer_registry_sync.snapshot import load_snapshot

    lv = _robotics(recording_linear, revenue=300000)
    plan = plan_deploy([desired(revenue=250000)], [lv])
    snaps = tmp_path / "snaps"
    client = recording_linear([lv], snapshot_dir=snaps)
    res = apply_deploy(plan, client, snapshot_dir=snaps)
    [call] = client.updates()
    assert call[4] == [res.snapshot.name]  # the snapshot existed at the first write
    [row] = load_snapshot(res.snapshot)
    assert (row.linear_id, row.revenue, row.updated_at) == ("lin-1", 300000, lv.updated_at)
    assert row.written_updated_at == client.rows["lin-1"].updated_at
    assert res.failure is None and res.created == []


def test_unwritable_snapshot_dir_aborts_before_any_write(tmp_path, recording_linear):
    import pytest

    lv = _robotics(recording_linear, revenue=300000)
    new = desired(name="New", domains=["new.com"], external_ids=["new"])
    plan = plan_deploy([new, desired(revenue=250000)], [lv])
    assert len(plan.creates) == 1 and len(plan.updates) == 1
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    client = recording_linear([lv])
    with pytest.raises(OSError):
        apply_deploy(plan, client, snapshot_dir=blocker)
    assert [c for c in client.calls if c[0] != "get"] == []


def test_update_sends_only_changed_fields(tmp_path, recording_linear):
    # live carries an extra domain and a tier the registry lacks: never sent
    lv = _robotics(
        recording_linear,
        domains=["acmerobotics.example.test", "acmerobotics.example.org"],
        revenue=300000,
        tier="Tier 2",
    )
    plan = plan_deploy([desired(revenue=250000, tier=None)], [lv])
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    [(_, cid, fields, retire, _)] = client.updates()
    assert (cid, fields, retire) == ("lin-1", {"revenue": 250000}, False)
    assert res.failure is None
    assert client.rows["lin-1"].domains == ["acmerobotics.example.test", "acmerobotics.example.org"]
    assert client.rows["lin-1"].tier == "Tier 2"

    # a retire-backed clear sends exactly that field as null, with retire=True
    lv2 = _robotics(recording_linear, tier="Tier 2")
    plan2 = plan_deploy([desired(tier=None)], [lv2], retire={"acmerobotics": {"tier": "Tier 2"}})
    client2 = recording_linear([lv2])
    res2 = apply_deploy(plan2, client2, snapshot_dir=tmp_path / "2")
    [(_, _, fields2, retire2, _)] = client2.updates()
    assert (fields2, retire2) == ({"tier_id": None}, True)
    assert res2.failure is None and client2.rows["lin-1"].tier is None


def test_verification_aborts_on_unplanned_field_change(tmp_path, recording_linear):
    a = _robotics(recording_linear, revenue=1)
    b = recording_linear.node(
        "lin-2", "Hooli", domains=["hooli.example.test"], revenue=1, xids=["hooli"]
    )
    da = desired(revenue=5)
    db = desired(
        name="Hooli", domains=["hooli.example.test"], revenue=5, tier=None, external_ids=["hooli"]
    )
    plan = plan_deploy([da, db], [a, b])
    assert [lv.id for _, lv in plan.updates] == ["lin-1", "lin-2"]
    client = recording_linear([a, b])
    client.tamper["lin-1"] = {"tier": "Tier 2"}  # Linear changed a field we did not send
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert res.failure == "Acme Robotics: tier expected Tier 1 got Tier 2"
    assert [c[1] for c in client.updates()] == ["lin-1"]  # Hooli never written
    assert client.rows["lin-2"].revenue == 1
    assert [p.linear_synced.linear_id for p in res.persisted] == ["lin-1"]


def test_verification_is_order_insensitive(tmp_path, recording_linear):
    lv = _robotics(recording_linear)
    plan = plan_deploy([desired(domains=["acmerobotics.example.test", "new.io"])], [lv])
    client = recording_linear([lv])
    # Linear returns the domains reordered and re-cased
    client.tamper["lin-1"] = {"domains": ["NEW.io", "AcmeRobotics.example.test"]}
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    [(_, _, fields, _, _)] = client.updates()
    assert fields == {"domains": ["acmerobotics.example.test", "new.io"]}
    assert res.failure is None


def test_verification_catches_planned_change_that_did_not_land(tmp_path, recording_linear):
    lv = _robotics(recording_linear, revenue=1)
    plan = plan_deploy([desired(revenue=5)], [lv])
    client = recording_linear([lv])
    client.tamper["lin-1"] = {"revenue": 1}
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert res.failure == "Acme Robotics: revenue expected 5 got 1"


# ---- pairing by identity ----

PRIORITY = [NOTION, LINEAR_FOUND]


def _table(tmp_path, *identities):
    """An identity table written by the real producer; each identity is
    (canonical id, names, domains, sources)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        assert store.add_identity(cid, names=names, domains=domains, sources=sources) == cid
    store.save()
    return load_identities(tmp_path / IDENTITIES_FILE)


def _node(
    lid,
    name,
    *,
    domains=(),
    revenue=None,
    tier=None,
    status="Active",
    xids=(),
    updated="2026-10-01T00:00:00.000Z",
):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": revenue,
            "externalIds": list(xids),
            "updatedAt": updated,
            "status": {"id": "s", "name": status} if status else None,
            "tier": {"id": "t", "name": tier} if tier else None,
        }
    )


def _rows(table, *entries):
    """Desired rows from the real merge; each entry is (dataset, source id, record)."""
    recs = [DatasetRecord(dataset=d, source_id=s, record=r) for d, s, r in entries]
    return merge_datasets(recs, table, PRIORITY).rows


def _acme(**kw):
    base = {
        "canonical_name": "Acme",
        "domains": ["acme.example.test"],
        "classification": "real",
        "arr": 5.0,
    }
    return CustomerRecord(**{**base, **kw})


def _initech_stub(**kw):
    base = {
        "canonical_name": "Initech",
        "domains": ["initech.example.test"],
        "classification": "real",
        "arr": 9.0,
        "linear_customer_id": "lin_initech",
        "sources": ["linear:lin_initech"],
    }
    return CustomerRecord(**{**base, **kw})


def test_one_live_per_identity_updates(tmp_path):
    table = _table(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}))
    [d] = _rows(table, (NOTION, "acme", _acme()))
    lv = _node("lin_a", "Acme [Active]", domains=["acme.example.test"], revenue=1, xids=["acme"])
    plan = plan_deploy([d], [lv], table)
    assert [lv_.id for _, lv_ in plan.updates] == ["lin_a"]
    assert [(c.field, c.old, c.new) for c in plan.changes["lin_a"]] == [("revenue", 1, 5)]
    assert not plan.creates and not plan.orphans and not plan.errors
    assert plan.known_duplicates == []


def test_two_live_writes_only_the_canonical_one_and_reports_the_other(tmp_path):
    table = _table(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}))
    [d] = _rows(table, (NOTION, "acme", _acme()))
    stamped = _node(
        "lin_a", "Acme [Active]", domains=["acme.example.test"], revenue=1, xids=["acme"]
    )
    other = _node("lin_b", "Acme Inc", domains=["acme.example.test"], revenue=1)
    plan = plan_deploy([d], [other, stamped], table)
    assert [lv.id for _, lv in plan.updates] == ["lin_a"]
    assert plan.known_duplicates == [("acme", other)]
    assert "lin_b" not in plan.changes  # the duplicate gets no change
    assert not plan.orphans and not plan.creates and not plan.errors and not plan.withheld


def test_two_live_falls_back_to_recorded_linear_id(tmp_path):
    table = _table(
        tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme", "linear": "lin_b"})
    )
    [d] = _rows(table, (NOTION, "acme", _acme()))
    first = _node("lin_a", "Acme", domains=["acme.example.test"], revenue=1)
    recorded = _node("lin_b", "Acme Inc", domains=["acme.example.test"], revenue=1)
    plan = plan_deploy([d], [first, recorded], table)
    assert [lv.id for _, lv in plan.updates] == ["lin_b"]
    assert plan.known_duplicates == [("acme", first)]
    assert ("external_ids", [], ["acme"]) in [
        (c.field, c.old, c.new) for c in plan.changes["lin_b"]
    ]
    assert "lin_a" not in plan.changes
    assert not plan.errors and not plan.orphans


def test_two_live_carrying_the_canonical_id_is_a_named_error(tmp_path):
    """Neither is picked by API order: the identity is not paired and nothing is written."""
    table = _table(
        tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme", "linear": "lin_a"})
    )
    [d] = _rows(table, (NOTION, "acme", _acme()))
    a = _node("lin_a", "Acme", domains=["acme.example.test"], revenue=1, xids=["acme"])
    b = _node("lin_b", "Acme Inc", domains=["acme.example.test"], revenue=1, xids=["acme"])
    expected = [
        (
            "two or more Linear customers carry the canonical id acme: "
            "'Acme' (lin_a), 'Acme Inc' (lin_b): merge them in Linear or remove the id "
            "from all but one"
        )
    ]
    for live in ([a, b], [b, a]):
        plan = plan_deploy([d], live, table)
        assert plan.errors == expected
        assert not plan.updates and not plan.creates and not plan.known_duplicates
        assert pick_primary("acme", live, table, schema=LINEAR_SCHEMA, spec=IDENTITY_SPEC) is None


def test_two_live_with_neither_id_is_an_error(tmp_path):
    table = _table(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}))
    [d] = _rows(table, (NOTION, "acme", _acme()))
    a = _node("lin_a", "Acme", domains=["acme.example.test"], revenue=1)
    b = _node("lin_b", "Acme Inc", domains=["acme.example.test"], revenue=1)
    plan = plan_deploy([d], [a, b], table)
    assert plan.errors == [
        (
            "known duplicates for acme with no canonical or recorded Linear id: "
            "'Acme' (lin_a), 'Acme Inc' (lin_b): merge them in Linear or record one in "
            "identities.yaml"
        )
    ]
    assert not plan.updates and not plan.creates and not plan.orphans and not plan.changes


def test_exclusivity_two_desired_rows_one_identity_is_an_error(tmp_path):
    table = _table(tmp_path, ("acme", ["Acme", "Acme Corp"], ["acme.example.test"], {}))
    rows = generate_linear(
        [_acme(), _acme(canonical_name="Acme Corp", domains=["acmecorp.example.test"])]
    )
    assert [r.external_ids for r in rows] == [["acme"], ["acmecorp"]]
    plan = plan_deploy(rows, [], table)
    assert plan.errors == [
        "two desired rows resolve to acme: 'Acme [Active]' and 'Acme Corp [Active]'"
    ]
    assert [c.name for c in plan.creates] == ["Acme [Active]"]


def test_withheld_shared_domain_is_reported_not_written(tmp_path):
    table = _table(
        tmp_path,
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
        ("beta", ["Beta"], ["beta.example.test"], {"notion": "beta"}),
        ("gamma", ["Gamma"], ["gamma.example.test"], {"notion": "gamma"}),
    )
    rows = _rows(
        table,
        (NOTION, "acme", _acme(domains=["acme.example.test", "shared.example.test"], arr=1.0)),
        (
            NOTION,
            "gamma",
            _acme(canonical_name="Gamma", domains=["gamma.example.test", "SHARED.example.test"]),
        ),
    )
    acme = _node("lin_a", "Acme [Active]", domains=["acme.example.test"], revenue=1, xids=["acme"])
    beta = _node("lin_b", "Beta [Active]", domains=["shared.example.test"], xids=["beta"])
    plan = plan_deploy(rows, [acme], table, other_live=[beta])
    assert plan.withheld == [
        (
            "withheld domain 'shared.example.test' for 'Acme [Active]': Linear customer 'Beta [Active]' "
            "already holds it (shared domains are unverified)"
        ),
        (
            "withheld domain 'SHARED.example.test' for 'Gamma [Active]': Linear customer 'Beta [Active]' "
            "already holds it (shared domains are unverified)"
        ),
    ]
    assert plan.changes["lin_a"] == [] and [t.name for t in plan.unchanged] == ["Acme [Active]"]
    [create] = plan.creates
    assert create.domains == ["gamma.example.test"]


def test_stub_row_writes_only_external_id(tmp_path):
    table = _table(
        tmp_path, ("initech", ["Initech"], ["initech.example.test"], {"linear": "lin_initech"})
    )
    [d] = _rows(table, (LINEAR_FOUND, "initech", _initech_stub()))
    assert d.linear_owned and d.revenue == 9 and d.name == "Initech"
    lv = _node(
        "lin_initech",
        "Initech",
        domains=["initech.example.test"],
        revenue=5,
        tier="Tier 2",
        status="Prospect",
    )
    plan = plan_deploy([d], [lv], table)
    assert plan.changes["lin_initech"] == [FieldChange("external_ids", [], ["initech"], "change")]
    [(target, _)] = plan.updates
    assert (target.name, target.revenue, target.tier, target.status, target.external_ids) == (
        "Initech",
        5,
        "Tier 2",
        "Prospect",
        ["initech"],
    )


def test_linear_owned_row_without_live_is_not_created(tmp_path):
    table = _table(
        tmp_path, ("initech", ["Initech"], ["initech.example.test"], {"linear": "lin_initech"})
    )
    [d] = _rows(table, (LINEAR_FOUND, "initech", _initech_stub()))
    plan = plan_deploy([d], [], table)
    assert plan.creates == [] and not plan.updates and not plan.errors
    assert plan.notes == ["stub 'Initech' has no Linear customer: run the refresh"]


def test_handoff_keeps_external_id_and_updates_name_status_revenue_tier(tmp_path):
    table = _table(
        tmp_path,
        (
            "initech",
            ["Initech"],
            ["initech.example.test"],
            {"linear": "lin_initech", "notion": "initech"},
        ),
    )
    yh = CustomerRecord(
        canonical_name="Initech Inc",
        domains=["initech.example.test"],
        classification="real",
        arr=9.0,
        tier="Tier 1",
    )
    [d] = _rows(table, (NOTION, "initech", yh), (LINEAR_FOUND, "initech", _initech_stub(arr=5.0)))
    assert not d.linear_owned and d.external_ids == ["initech"]
    # the stub was built and stamped earlier: live carries the canonical id
    lv = _node(
        "lin_initech",
        "Initech",
        domains=["initech.example.test"],
        revenue=5,
        tier="Tier 2",
        status="Prospect",
        xids=["initech"],
    )
    plan = plan_deploy([d], [lv], table)
    changes = plan.changes["lin_initech"]
    assert {c.field for c in changes} == {"name", "status", "revenue", "tier"}
    assert not [c for c in changes if c.field == "external_ids"]


def test_merge_by_hand_restamps_one_row(tmp_path):
    table = _table(
        tmp_path, ("new-id", ["Acme"], ["acme.example.test"], {"notion": "acme", "linear": "lin_1"})
    )
    [d] = _rows(table, (NOTION, "acme", _acme(arr=1.0)))
    lv = _node("lin_1", "Acme [Active]", domains=["acme.example.test"], revenue=1, xids=["old-id"])
    plan = plan_deploy([d], [lv], table)
    assert plan.changes["lin_1"] == [FieldChange("external_ids", ["old-id"], ["new-id"], "change")]
    assert not plan.creates and not plan.orphans and not plan.errors


# ---- ignore list at write time; apply guards ----


def _ignore_list(tmp_path, text):
    from gitops_engine.ignore import load_ignore

    from customer_registry_sync.linear_files import IGNORE_FILE, IGNORE_FORMAT

    (tmp_path / IGNORE_FILE).write_text(text)
    return load_ignore(tmp_path / IGNORE_FILE, IGNORE_FORMAT)


def test_apply_never_writes_ignored_row_unscoped(tmp_path, recording_linear):
    table = _table(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}))
    [d] = _rows(table, (NOTION, "acme", _acme(arr=9.0)))
    lv = recording_linear.node(
        "lin_1", "Acme [Active]", domains=["acme.example.test"], revenue=1, xids=["acme"]
    )
    plan = plan_deploy([d], [lv], table)
    assert len(plan.updates) == 1
    # between plan and write, someone adds Acme to the ignore list
    ign = _ignore_list(tmp_path, "customers:\n  - linear_id: lin_1\n    reason: now a test org\n")
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path / "snaps", ignore=ign)
    assert client.updates() == [] and res.failure is None
    assert [(t.name, why) for t, _, why in plan.aborts] == [
        ("Acme [Active]", "ignored at write time: now a test org")
    ]


def test_apply_writes_zzz_row_when_scoped(tmp_path, recording_linear):
    from gitops_engine.ignore import BUILTIN_PREFIX, load_ignore

    from customer_registry_sync.linear_files import IGNORE_FORMAT

    scope = f"{BUILTIN_PREFIX}1a2b3c4d-"
    rows = generate_linear([_acme(canonical_name=f"{scope}Acme", arr=9.0)])
    ign = load_ignore(tmp_path / "missing.yaml", IGNORE_FORMAT)  # the built-in prefix only

    def run(scope_arg):
        lv = recording_linear.node(
            "lin_1",
            rows[0].name,
            domains=["acme.example.test"],
            revenue=1,
            xids=rows[0].external_ids,
        )
        plan = plan_deploy(rows, [lv], IdentityTable())
        client = recording_linear([lv])
        apply_deploy(plan, client, snapshot_dir=tmp_path / "snaps", ignore=ign, scope=scope_arg)
        return plan, client

    plan, client = run(scope)
    [(_, lid, fields, _, _)] = client.updates()
    assert (lid, fields) == ("lin_1", {"revenue": 9}) and plan.aborts == []
    plan, client = run(None)  # an unscoped apply never writes the harness's rows
    assert client.updates() == []
    assert [why for _, _, why in plan.aborts] == [
        "ignored at write time: live e2e harness throwaway records (built in; always present)"
    ]


def test_apply_refuses_plan_with_errors(tmp_path, recording_linear):
    import pytest

    table = _table(tmp_path, ("acme", ["Acme", "Acme Corp"], ["acme.example.test"], {}))
    rows = generate_linear(
        [_acme(), _acme(canonical_name="Acme Corp", domains=["acmecorp.example.test"])]
    )
    plan = plan_deploy(rows, [], table)
    assert plan.errors and plan.creates
    client = recording_linear([])
    snaps = tmp_path / "snaps"
    with pytest.raises(ValueError, match="planner error"):
        apply_deploy(plan, client, snapshot_dir=snaps)
    assert client.calls == [] and not snaps.exists()


def test_stub_with_empty_live_status_applies_cleanly(tmp_path, recording_linear):
    table = _table(
        tmp_path, ("initech", ["Initech"], ["initech.example.test"], {"linear": "lin_initech"})
    )
    [d] = _rows(table, (LINEAR_FOUND, "initech", _initech_stub()))
    lv = recording_linear.node(
        "lin_initech", "Initech", domains=["initech.example.test"], revenue=5, status=None
    )
    assert lv.status == ""
    plan = plan_deploy([d], [lv], table)
    # no status write is planned (a row needs a status, so the target holds a placeholder)
    assert plan.changes["lin_initech"] == [FieldChange("external_ids", [], ["initech"], "change")]
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path / "snaps")
    assert res.failure is None and not plan.aborts
    [(_, _, fields, _, _)] = client.updates()
    assert fields == {"external_ids": ["initech"]}
