"""Field-level planner: ownership rules and the destructive guard."""

import itertools

from customer_registry_sync.linear_files import CuratedFields
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetRecord
from customer_registry_sync.fieldplan import (
    DESTRUCTIVE,
    FieldChange,
    Untracked,
    plan_row,
    stale_retire,
    state_to_live,
    unacknowledged_destructive,
    untracked_values,
)
from customer_registry_sync.generate import merge_datasets
from customer_registry_sync.identity import IdentityTable
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced
from customer_registry_sync.schema import CustomerRecord


def live(
    domains=(),
    revenue=None,
    tier=None,
    name="Acme [Active]",
    status="Active",
    external_ids=("acme",),
):
    """A live row built through the real GraphQL node parser."""
    return parse_customer_node(
        {
            "id": "lin_1",
            "name": name,
            "domains": list(domains),
            "revenue": revenue,
            "externalIds": list(external_ids),
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s1", "name": status},
            "tier": {"id": "t1", "name": tier} if tier else None,
        }
    )


def want(
    domains=(),
    revenue=None,
    tier=None,
    name="Acme [Active]",
    status="Active",
    external_ids=("acme",),
):
    return LinearCustomer(
        name=name,
        domains=list(domains),
        revenue=revenue,
        tier=tier,
        status=status,
        external_ids=list(external_ids),
    )


def test_registry_empty_domains_keep_live():
    target, changes = plan_row(want(domains=[]), live(domains=["stratex.example.test"]), None)
    assert target.domains == ["stratex.example.test"]
    assert changes == []


def test_registry_none_revenue_keeps_live():
    target, changes = plan_row(want(revenue=None), live(revenue=300000), None)
    assert target.revenue == 300000
    assert changes == []


def test_registry_revenue_overrides_live():
    target, changes = plan_row(want(revenue=5000000), live(revenue=5500000), None)
    assert target.revenue == 5000000
    assert [(c.field, c.old, c.new, c.kind) for c in changes] == [
        ("revenue", 5500000, 5000000, "change")
    ]
    assert unacknowledged_destructive(changes) == []


def test_domain_add_and_union():
    target, changes = plan_row(want(domains=["a.com"]), live(domains=["b.io"]), None)
    assert sorted(target.domains) == ["a.com", "b.io"]
    assert [(c.field, c.kind, c.new) for c in changes] == [("domains", "add", "a.com")]


def test_domain_case_is_not_a_change():
    target, changes = plan_row(want(domains=["acme.com"]), live(domains=["ACME.com"]), None)
    assert target.domains == ["ACME.com"]  # live casing kept
    assert changes == []


def test_retire_domain_removes():
    target, changes = plan_row(
        want(domains=["a.com"]), live(domains=["a.com", "b.io"]), CuratedFields(domains=["b.io"])
    )
    assert target.domains == ["a.com"]
    [c] = changes
    assert (c.field, c.old, c.new, c.kind, c.retire) == ("domains", "b.io", None, "remove", True)
    assert unacknowledged_destructive(changes) == []


def test_retire_revenue_clears_only_on_exact_match():
    target, changes = plan_row(want(), live(revenue=300000), CuratedFields(revenue=300000))
    assert target.revenue is None
    [c] = changes
    assert (c.field, c.old, c.new, c.kind, c.retire) == ("revenue", 300000, None, "clear", True)

    target, changes = plan_row(want(), live(revenue=250000), CuratedFields(revenue=300000))
    assert target.revenue == 250000
    assert changes == []


def test_external_ids_change_is_not_destructive():
    target, changes = plan_row(want(external_ids=["acme"]), live(external_ids=["acme-old"]), None)
    assert target.external_ids == ["acme"]
    [c] = changes
    assert (c.field, c.old, c.new, c.kind) == ("external_ids", ["acme-old"], ["acme"], "change")
    assert c.kind not in DESTRUCTIVE
    assert unacknowledged_destructive(changes) == []


def test_property_no_destructive_without_retire():
    domain_pool = [[], ["a.com"], ["a.com", "b.io"]]
    revenue_pool = [None, 1, 2]
    tier_pool = [None, "Tier 1"]
    for dd, ld, dr, lr, dt, lt in itertools.product(
        domain_pool, domain_pool, revenue_pool, revenue_pool, tier_pool, tier_pool
    ):
        _, changes = plan_row(
            want(domains=dd, revenue=dr, tier=dt), live(domains=ld, revenue=lr, tier=lt), None
        )
        assert unacknowledged_destructive(changes) == [], (dd, ld, dr, lr, dt, lt)
        assert not [c for c in changes if c.kind in DESTRUCTIVE], (dd, ld, dr, lr, dt, lt)


def test_property_destructive_only_from_matching_retire():
    """Across registry x live x retire combinations: every remove/clear is backed by
    a retire entry whose value is live right now; a retired value never survives in
    the target; and no live value the retire entry does not name is ever lost."""
    domain_pool = [[], ["a.com"], ["a.com", "b.io"]]
    revenue_pool = [None, 1, 2]
    tier_pool = [None, "Tier 1"]
    retire_pool = [
        None,
        CuratedFields(domains=["b.io"]),
        CuratedFields(domains=["B.IO"]),
        CuratedFields(revenue=1),
        CuratedFields(tier="Tier 1"),
        CuratedFields(domains=["a.com"], revenue=2, tier="Tier 1"),
    ]
    for dd, ld, dr, lr, dt, lt, rt in itertools.product(
        domain_pool, domain_pool, revenue_pool, revenue_pool, tier_pool, tier_pool, retire_pool
    ):
        case = (dd, ld, dr, lr, dt, lt, rt)
        target, changes = plan_row(
            want(domains=dd, revenue=dr, tier=dt), live(domains=ld, revenue=lr, tier=lt), rt
        )
        assert unacknowledged_destructive(changes) == [], case
        retired_doms = {d.lower() for d in (rt.domains if rt else [])}
        live_doms = {d.lower() for d in ld}
        for c in changes:
            if c.kind == "remove":
                assert c.retire and c.old.lower() in retired_doms & live_doms, case
            if c.kind == "clear":
                assert c.retire and getattr(rt, c.field) == c.old, case
                assert {"revenue": lr, "tier": lt}[c.field] == c.old, case
        target_doms = {d.lower() for d in target.domains}
        assert not (retired_doms & target_doms), case
        assert live_doms - retired_doms <= target_doms, case
        for field, live_val in (("revenue", lr), ("tier", lt)):
            retired_val = getattr(rt, field) if rt else None
            if live_val is not None and retired_val != live_val:
                assert getattr(target, field) is not None, case


def test_unacknowledged_destructive_flags_retire_false():
    from customer_registry_sync.fieldplan import FieldChange

    bad = FieldChange("revenue", 1, None, "clear")
    ok = FieldChange("revenue", 1, None, "clear", retire=True)
    assert unacknowledged_destructive([bad, ok, FieldChange("name", "a", "b", "change")]) == [bad]


def test_untracked_reports_live_only_domain_and_revenue():
    got = untracked_values(
        [want(domains=["a.com"])],
        [live(domains=["a.com", "stratex.example.test"], revenue=300000, tier="Tier 1")],
        IdentityTable(),
        {},
    )
    assert got == [
        Untracked("acme", "Acme [Active]", "domains", "stratex.example.test"),
        Untracked("acme", "Acme [Active]", "revenue", 300000),
        Untracked("acme", "Acme [Active]", "tier", "Tier 1"),
    ]


def test_untracked_ignores_case():
    assert (
        untracked_values(
            [want(domains=["acme.com"])], [live(domains=["ACME.com"])], IdentityTable(), {}
        )
        == []
    )


def test_untracked_ignores_values_the_registry_carries():
    assert (
        untracked_values(
            [want(revenue=5, tier="Tier 2")],
            [live(revenue=300000, tier="Tier 1")],
            IdentityTable(),
            {},
        )
        == []
    )


def test_untracked_excludes_retired():
    retire = {"acme": CuratedFields(domains=["OLD.io"], revenue=300000, tier="Tier 1")}
    got = untracked_values(
        [want()], [live(domains=["old.io"], revenue=300000, tier="Tier 1")], IdentityTable(), retire
    )
    assert got == []
    # a retire entry for a different revenue does not hide the live one
    got = untracked_values([want()], [live(revenue=250000)], IdentityTable(), retire)
    assert [(u.field, u.value) for u in got] == [("revenue", 250000)]


def test_untracked_ignores_orphans():
    other = live(name="Other [Active]", external_ids=("other",), domains=["x.com"])
    assert untracked_values([want()], [other], IdentityTable(), {}) == []


def test_stale_retire_unknown_xid_and_absent_value():
    retire = {
        "ghost": CuratedFields(domains=["a.com"]),
        "nolive": CuratedFields(revenue=1),
        "acme": CuratedFields(domains=["gone.io", "b.io"], revenue=7, tier="Tier 9"),
    }
    desired = [want(), want(name="Nolive [Active]", external_ids=("nolive",))]
    msgs = stale_retire(
        desired, [live(domains=["b.io"], revenue=300000, tier="Tier 1")], IdentityTable(), retire
    )
    assert msgs == [
        "ghost: no registry row",
        "nolive: no live match",
        "acme: domains gone.io not live",
        "acme: revenue 7 not live",
        "acme: tier Tier 9 not live",
    ]


def test_stale_retire_clean_entry_is_silent():
    retire = {"acme": CuratedFields(domains=["B.io"], revenue=300000)}
    assert (
        stale_retire([want()], [live(domains=["b.io"], revenue=300000)], IdentityTable(), retire)
        == []
    )


def test_stale_retire_conflicts_with_registry():
    retire = {"acme": CuratedFields(domains=["A.com"], revenue=5, tier="Tier 1")}
    msgs = stale_retire(
        [want(domains=["a.com"], revenue=5, tier="Tier 1")],
        [live(domains=["a.com"], revenue=5, tier="Tier 1")],
        IdentityTable(),
        retire,
    )
    assert msgs == [
        "acme: domains A.com conflicts with registry",
        "acme: revenue 5 conflicts with registry",
        "acme: tier Tier 1 conflicts with registry",
    ]


def test_stale_retire_conflicts_when_registry_sets_field():
    retire = {"acme": CuratedFields(revenue=300000)}
    msgs = stale_retire([want(revenue=250000)], [live(revenue=300000)], IdentityTable(), retire)
    assert msgs == ["acme: revenue 300000 conflicts with registry"]


def test_state_to_live_uses_linear_synced():
    rec = want(domains=["a.com"], revenue=3, tier="Tier 1")
    rec.linear_synced = LinearSynced("lin_9", "2026-10-02T00:00:00.000Z", "h")
    lv = state_to_live(rec)
    assert (
        lv.id,
        lv.updated_at,
        lv.name,
        lv.domains,
        lv.revenue,
        lv.tier,
        lv.status,
        lv.external_ids,
    ) == (
        "lin_9",
        "2026-10-02T00:00:00.000Z",
        "Acme [Active]",
        ["a.com"],
        3,
        "Tier 1",
        "Active",
        ["acme"],
    )


def _initech_stub_row(**kw) -> LinearCustomer:
    """A `linear-found` stub's desired row, built by the real merge."""
    base = {
        "canonical_name": "Initech",
        "domains": ["initech.example.test"],
        "classification": "real",
        "arr": 5.0,
        "linear_customer_id": "lin_initech",
        "sources": ["linear:lin_initech"],
    }
    rec = CustomerRecord(**{**base, **kw})
    [row] = merge_datasets(
        [DatasetRecord(dataset=LINEAR_FOUND, source_id="initech", record=rec)],
        IdentityTable(),
        [NOTION, LINEAR_FOUND],
    ).rows
    assert row.linear_owned and row.external_ids == ["initech"]
    return row


def _initech_live(**kw):
    node = {
        "id": "lin_initech",
        "name": "Initech",
        "domains": ["initech.example.test"],
        "revenue": 5,
        "externalIds": [],
        "updatedAt": "2026-10-01T00:00:00.000Z",
        "status": {"id": "s1", "name": "Active"},
        "tier": None,
    }
    return parse_customer_node({**node, **kw})


def test_linear_edit_to_stub_is_not_reverted():
    d = _initech_stub_row()
    # after the stub was built, someone renamed the customer in Linear and changed revenue
    lv = _initech_live(
        name="Initech Systems", revenue=7, domains=["initech.example.test", "initech.example.org"]
    )
    target, changes = plan_row(d, lv, None)
    assert changes == [FieldChange("external_ids", [], ["initech"], "change")]
    assert (target.name, target.revenue, target.domains, target.external_ids) == (
        "Initech Systems",
        7,
        ["initech.example.test", "initech.example.org"],
        ["initech"],
    )
    # a retire entry never reaches a Linear-owned row
    _, changes = plan_row(d, lv, CuratedFields(domains=["initech.example.org"], revenue=7))
    assert changes == [FieldChange("external_ids", [], ["initech"], "change")]


def test_untracked_and_stale_retire_skip_linear_owned_rows():
    d = _initech_stub_row(arr=None)
    lv = _initech_live(
        domains=["initech.example.test", "initech.example.org"],
        revenue=7,
        tier={"id": "t1", "name": "Tier 1"},
    )
    assert untracked_values([d], [lv], IdentityTable(), {}) == []
    assert stale_retire([d], [lv], IdentityTable(), {"initech": CuratedFields(revenue=7)}) == [
        "initech: Linear-owned stub (retire does not apply)"
    ]
