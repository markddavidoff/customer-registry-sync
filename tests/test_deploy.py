from customer_registry_sync.deploy import _match_live, apply_deploy, live_to_linear, plan_deploy
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of


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


def test_alias_links_desired_to_live_instead_of_dup_create():
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
    # without the alias: a duplicate create + a false orphan
    plan0 = plan_deploy([d], live, aliases={})
    assert len(plan0.creates) == 1 and len(plan0.orphans) == 1
    # with the alias: an update, no create, no orphan
    plan = plan_deploy([d], live, aliases={"Stark Fabrication LLC (The Forge)": "The Forge"})
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


def test_apply_issues_create_and_update_only_and_writes_back_linear_synced():
    create = desired(name="New", domains=["new.com"], external_ids=["new"], linear_synced=None)
    update = desired(
        revenue=999999, linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "x")
    )
    live = [livec(revenue=250000)]  # matches `update` by external id, pre-state matches
    plan = plan_deploy([create, update], live=live)
    assert len(plan.creates) == 1 and len(plan.updates) == 1

    client = FakeClient(live_now=live)
    persisted = apply_deploy(plan, client)
    assert len(client.created) == 1 and len(client.updated) == 1
    # linear_synced written back from the write result
    by_name = {r.name: r for r in persisted}
    assert by_name["New"].linear_synced.linear_id == "new-id"
    assert by_name["Acme Robotics"].linear_synced.updated_at == "2026-11-02T00:00:00.000Z"
    # content_hash recomputed on the persisted record
    assert by_name["Acme Robotics"].linear_synced.content_hash == content_hash_of(
        by_name["Acme Robotics"]
    )


def test_apply_aborts_on_write_time_drift():
    # plan says update, but between plan and apply the live row drifted
    update = desired(
        revenue=999999, linear_synced=LinearSynced("lin-1", "2026-10-07T00:00:00.000Z", "x")
    )
    plan = plan_deploy([update], live=[livec(revenue=250000)])
    assert len(plan.updates) == 1
    # client's get_customer now reports a DIFFERENT updatedAt -> write-time drift
    client = FakeClient(live_now=[livec(updated_at="2026-12-31T00:00:00.000Z")])
    apply_deploy(plan, client)
    assert not client.updated  # nothing written


def test_apply_aborts_first_link_row_on_write_time_drift():
    # first-link row (linear_synced is None) must ALSO be guarded by check-and-set
    first_link = desired(revenue=999999, linear_synced=None)
    plan = plan_deploy([first_link], live=[livec(revenue=250000)])  # match by external id -> update
    assert len(plan.updates) == 1
    # live drifted between plan (updatedAt 2026-10-07) and apply (2026-12-31)
    client = FakeClient(live_now=[livec(updated_at="2026-12-31T00:00:00.000Z")])
    apply_deploy(plan, client)
    assert not client.updated  # must NOT clobber the concurrently-edited row


def test_apply_preflight_writes_nothing_on_unresolvable_status():
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
        apply_deploy(plan, client)
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
    s = live_to_linear(lv)
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


def test_apply_never_deletes_orphans():
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
    assert apply_deploy(plan, C()) == [] and deleted == []


def test_persisted_state_hashes_what_linear_returned_not_desired():
    """Seam: the state mirror must come from the row Linear RETURNED, because
    check/plan_deploy/sync-inbound hash the live row, not the desired one."""
    from customer_registry_sync.deploy import DeployPlan

    want = LinearCustomer(
        name="Acme", domains=["ACME.com"], revenue=5, status="Active", external_ids=["acme"]
    )
    live0 = _live("lin_1", "Acme", "2026-10-01T00:00:00.000Z", revenue=1)
    returned = LiveCustomer(
        id="lin_1",
        name="Acme",
        domains=["acme.example.test"],
        revenue=10,
        tier=None,
        status="Active",
        external_ids=["acme"],
        updated_at="2026-10-03T00:00:00.000Z",
    )

    class Normalizing:
        def status_ids(self):
            return {"Active": "s"}

        def tier_ids(self):
            return {}

        def get_customer(self, cid):
            return live0

        def update_customer(self, cid, **f):
            # Linear "normalizes": lowercases domains, rounds revenue
            assert f["domains"] == ["ACME.com"] and f["revenue"] == 5
            return returned

    [st] = apply_deploy(DeployPlan(updates=[(want, live0)]), Normalizing())
    assert st.domains == ["acme.example.test"] and st.revenue == 10
    assert st.linear_synced.updated_at == returned.updated_at
    assert st.linear_synced.content_hash == content_hash_of(live_to_linear(returned))
    assert st.linear_synced.content_hash != content_hash_of(want)


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
        assert _match_live(d, [lv]) is lv
    plain = desired(name="Gringotts Advisory", domains=[], external_ids=[], linear_synced=None)
    lv = livec(id="y", name="Gringotts Advisory [Active]", domains=[], external_ids=[])
    assert _match_live(plain, [lv]) is lv


def test_name_fallback_does_not_match_different_names_or_non_status_brackets():
    d = desired(name="Acme [Active]", domains=[], external_ids=[], linear_synced=None)
    assert _match_live(d, [livec(id="a", name="Acme [Beta]", domains=[], external_ids=[])]) is None
    assert (
        _match_live(d, [livec(id="b", name="Other [Active]", domains=[], external_ids=[])]) is None
    )


def test_alias_still_links_when_desired_name_is_suffixed():
    d = desired(
        name="Stark Fabrication LLC (The Forge) [Active]",
        domains=[],
        external_ids=["stark-forge"],
        linear_synced=None,
    )
    live = [
        livec(
            id="lin-forge", name="The Forge", domains=["starkforge.example.test"], external_ids=[]
        )
    ]
    plan = plan_deploy([d], live, aliases={"Stark Fabrication LLC (The Forge)": "The Forge"})
    assert len(plan.updates) == 1 and not plan.creates and not plan.orphans
    live2 = [
        livec(
            id="lin-forge",
            name="The Forge [Active]",
            domains=["starkforge.example.test"],
            external_ids=[],
        )
    ]
    plan2 = plan_deploy([d], live2, aliases={"Stark Fabrication LLC (The Forge)": "The Forge"})
    assert len(plan2.updates) == 1 and not plan2.creates and not plan2.orphans
