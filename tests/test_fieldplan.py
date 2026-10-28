"""The Linear field policies, declared on the engine planner, against Linear-shaped rows.

The policy mechanics are engine tests (`gitops_engine/tests/test_fieldplan.py`); these
pin the declarations and the customer-specific cases: domain casing, the
`linear-found` stub built by the real merge, and the mirror's live view.
"""

from gitops_engine.fieldplan import FieldChange, plan_row, stale_retire, untracked_values
from gitops_engine.schema import Always, KeepIfUnset, Union

from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetRecord
from customer_registry_sync.generate import merge_datasets
from customer_registry_sync.identity import IDENTITY_SPEC, IdentityTable, implicit_identity
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_schema import LINEAR_SCHEMA, LinearCustomer, LinearSynced
from customer_registry_sync.schema import CustomerRecord

BOUND = dict(schema=LINEAR_SCHEMA, spec=IDENTITY_SPEC, implicit=implicit_identity)


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


def test_linear_field_policies():
    assert [(f.name, f.policy, f.required) for f in LINEAR_SCHEMA.fields] == [
        ("name", Always(), False),
        ("status", Always(), True),
        ("external_ids", Always(unordered=True), False),
        ("domains", Union(exclusive=True), False),
        ("revenue", KeepIfUnset(), False),
        ("tier", KeepIfUnset(), False),
    ]
    assert LINEAR_SCHEMA.id_field == "external_ids"
    assert (LINEAR_SCHEMA.nouns.record, LINEAR_SCHEMA.nouns.records) == ("customer", "customers")
    assert LINEAR_SCHEMA.nouns.destination == "Linear"


def test_domain_case_is_not_a_change():
    target, changes = plan_row(
        want(domains=["acme.com"]), live(domains=["ACME.com"]), None, schema=LINEAR_SCHEMA
    )
    assert target.domains == ["ACME.com"]  # live casing kept
    assert changes == []


def test_retire_domain_removes_and_tier_clears():
    target, changes = plan_row(
        want(domains=["a.com"]),
        live(domains=["a.com", "b.io"], tier="Tier 1"),
        {"domains": ["B.IO"], "revenue": None, "tier": "Tier 1"},
        schema=LINEAR_SCHEMA,
    )
    assert (target.domains, target.tier) == (["a.com"], None)
    assert changes == [
        FieldChange("domains", "b.io", None, "remove", retire=True),
        FieldChange("tier", "Tier 1", None, "clear", retire=True),
    ]


def test_state_to_live_uses_linear_synced():
    rec = want(domains=["a.com"], revenue=3, tier="Tier 1")
    rec.linear_synced = LinearSynced("lin_9", "2026-10-02T00:00:00.000Z", "h")
    lv = LINEAR_SCHEMA.state_to_live(rec)
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
    target, changes = plan_row(d, lv, None, schema=LINEAR_SCHEMA)
    assert changes == [FieldChange("external_ids", [], ["initech"], "change")]
    assert (target.name, target.revenue, target.domains, target.external_ids) == (
        "Initech Systems",
        7,
        ["initech.example.test", "initech.example.org"],
        ["initech"],
    )
    # a retire entry never reaches a Linear-owned row
    _, changes = plan_row(
        d, lv, {"domains": ["initech.example.org"], "revenue": 7}, schema=LINEAR_SCHEMA
    )
    assert changes == [FieldChange("external_ids", [], ["initech"], "change")]


def test_untracked_and_stale_retire_skip_linear_owned_rows():
    d = _initech_stub_row(arr=None)
    lv = _initech_live(
        domains=["initech.example.test", "initech.example.org"],
        revenue=7,
        tier={"id": "t1", "name": "Tier 1"},
    )
    assert untracked_values([d], [lv], IdentityTable(), {}, **BOUND) == []
    assert stale_retire([d], [lv], IdentityTable(), {"initech": {"revenue": 7}}, **BOUND) == [
        "initech: Linear-owned stub (retire does not apply)"
    ]
