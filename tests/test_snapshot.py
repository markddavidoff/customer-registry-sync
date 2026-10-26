from dataclasses import replace
from datetime import UTC, datetime

import yaml

from customer_registry_sync.snapshot import (
    load_snapshot,
    record_written,
    restore,
    write_snapshot,
)


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


def test_write_load_and_record_round_trip(tmp_path, recording_linear):
    lv = _row(recording_linear, tier="Tier 2")
    when = datetime(2026, 10, 22, 9, 8, 7, tzinfo=UTC)
    path = write_snapshot([lv], tmp_path / "snaps", now=when)
    assert path == tmp_path / "snaps" / "20261022T090807Z.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["taken_at"] == "2026-10-22T09:08:07Z" and len(raw["rows"]) == 1
    record_written(path, "lin-1", "2026-11-01T00:00:00.000Z")
    [row] = load_snapshot(path)
    assert (
        row.linear_id,
        row.name,
        row.domains,
        row.revenue,
        row.tier,
        row.status,
        row.external_ids,
        row.updated_at,
        row.written_updated_at,
    ) == (
        "lin-1",
        "Acme",
        ["acme.com"],
        300000,
        "Tier 2",
        "Active",
        ["acme"],
        lv.updated_at,
        "2026-11-01T00:00:00.000Z",
    )


def test_second_snapshot_in_same_second_does_not_overwrite(tmp_path, recording_linear):
    when = datetime(2026, 10, 22, 9, 8, 7, tzinfo=UTC)
    first = write_snapshot([_row(recording_linear)], tmp_path, now=when)
    second = write_snapshot([], tmp_path, now=when)
    assert first != second and load_snapshot(first) and load_snapshot(second) == []


def test_restore_skips_row_gone(tmp_path, recording_linear):
    lv = _row(recording_linear)
    path = write_snapshot([lv], tmp_path)
    client = recording_linear([])
    out = restore(path, client)
    assert out.restored == [] and out.skipped == [("lin-1", "gone")]


# Stand-ins for the source's apply-driven restore tests (they need `apply_deploy`,
# ported in Task 5). They build the snapshot with the real writer and
# `record_written`, which is what `apply_deploy` calls, so the restore path is
# still covered end to end here.
def test_restore_writes_snapshot_values_back_and_retires_empty_fields(tmp_path, recording_linear):
    before = _row(recording_linear)  # tier None: restore must send it as an explicit retire
    path = write_snapshot([before], tmp_path)
    client = recording_linear([before])
    changed = client.update_customer("lin-1", revenue=5, tier_id="t1")  # an apply
    record_written(path, "lin-1", changed.updated_at)

    out = restore(path, client)
    assert out.restored == ["lin-1"] and out.skipped == [] and out.failure is None
    assert replace(client.rows["lin-1"], updated_at=before.updated_at) == before
    *_, (_, _, fields, retire, _) = client.updates()
    assert fields["tier_id"] is None and retire is True


def test_restore_filters_rows_by_external_id(tmp_path, recording_linear):
    lv = _row(recording_linear)
    path = write_snapshot([lv], tmp_path)
    client = recording_linear([lv])
    changed = client.update_customer("lin-1", revenue=5)
    record_written(path, "lin-1", changed.updated_at)
    out = restore(path, client, rows=["someone-else"])
    assert out.restored == [] and out.skipped == []
    assert client.rows["lin-1"].revenue == 5
    out = restore(path, client, rows=["acme"])
    assert out.restored == ["lin-1"] and client.rows["lin-1"].revenue == 300000


def test_restore_skips_row_edited_since_apply(tmp_path, recording_linear):
    lv = _row(recording_linear)
    path = write_snapshot([lv], tmp_path)
    client = recording_linear([lv])
    changed = client.update_customer("lin-1", revenue=5)
    record_written(path, "lin-1", changed.updated_at)
    client.update_customer("lin-1", revenue=77)  # someone edits after the apply
    n_writes = len(client.updates())
    out = restore(path, client)
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
