"""Restore through the customer bindings. The snapshot mechanics themselves are
tested in gitops-engine (`tests/test_snapshot.py`)."""

from dataclasses import replace
from functools import partial

from gitops_engine import snapshot

from customer_registry_sync.deploy import apply_deploy, plan_deploy, restore
from customer_registry_sync.linear_schema import LINEAR_SCHEMA, LinearCustomer

write_snapshot = partial(snapshot.write_snapshot, schema=LINEAR_SCHEMA)


def _row(rl, **kw):
    base = {
        "domains": ["acme.com"],
        "revenue": 300000,
        "tier": None,
        "xids": ["acme"],
        "updated": "2026-10-07T00:00:00.000Z",
    }
    base.update(kw)
    return rl.node("lin-1", "Acme", **base)


def _desired(**kw):
    base = {
        "name": "Acme",
        "domains": ["acme.com"],
        "revenue": 5,
        "status": "Active",
        "external_ids": ["acme"],
    }
    base.update(kw)
    return LinearCustomer(**base)


def test_restore_skips_row_gone(tmp_path, recording_linear):
    lv = _row(recording_linear)
    path = write_snapshot([lv], tmp_path)
    client = recording_linear([])
    out = restore(path, client)
    assert out.restored == [] and out.skipped == [("lin-1", "gone")]


def test_restore_round_trip(tmp_path, recording_linear):
    before = _row(recording_linear)  # tier None: restore must clear what apply set
    plan = plan_deploy([_desired(revenue=5, tier="Tier 1")], [before])
    client = recording_linear([before])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    assert res.failure is None
    assert (client.rows["lin-1"].revenue, client.rows["lin-1"].tier) == (5, "Tier 1")

    out = restore(res.snapshot, client)
    assert out.restored == ["lin-1"] and out.skipped == []
    after = client.rows["lin-1"]
    assert replace(after, updated_at=before.updated_at) == before
    # the empty tier went out as an explicit retire, not a silent drop
    *_, (_, _, fields, retire, _) = client.updates()
    assert fields["tier_id"] is None and retire is True


def test_restore_filters_rows_by_external_id(tmp_path, recording_linear):
    lv = _row(recording_linear)
    plan = plan_deploy([_desired()], [lv])
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    out = restore(res.snapshot, client, rows=["someone-else"])
    assert out.restored == [] and out.skipped == []
    assert client.rows["lin-1"].revenue == 5
    out = restore(res.snapshot, client, rows=["acme"])
    assert out.restored == ["lin-1"] and client.rows["lin-1"].revenue == 300000


def test_restore_skips_row_edited_since_apply(tmp_path, recording_linear):
    lv = _row(recording_linear)
    plan = plan_deploy([_desired()], [lv])
    client = recording_linear([lv])
    res = apply_deploy(plan, client, snapshot_dir=tmp_path)
    client.update_customer("lin-1", revenue=77)  # someone edits after the apply
    n_writes = len(client.updates())
    out = restore(res.snapshot, client)
    assert out.restored == []
    [(lid, reason)] = out.skipped
    assert lid == "lin-1" and "edited since" in reason
    assert len(client.updates()) == n_writes and client.rows["lin-1"].revenue == 77


def test_restore_stops_at_first_linear_error(tmp_path, recording_linear):
    from customer_registry_sync.linear_client import LinearError

    a = recording_linear.node("lin-a", "Acme", xids=["acme"])
    b = recording_linear.node("lin-b", "Beta", xids=["beta"])
    path = write_snapshot([a, b], tmp_path)
    client = recording_linear([a, b])
    client.fail["lin-a"] = LinearError("boom")
    out = restore(path, client)
    assert out.restored == [] and out.failure == "lin-a: boom"
    assert [c[1] for c in client.updates()] == ["lin-a"]  # lin-b was never attempted
