from customer_registry_sync.check import detect_drift, offline_errors, run_check
from customer_registry_sync.datasets import (
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    dataset_dir,
    load_datasets,
)
from customer_registry_sync.generate import generate_linear
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities
from customer_registry_sync.identity_store import IdentityStore
from customer_registry_sync.inbound_sync import reconcile_inbound
from customer_registry_sync.linear_client import LiveCustomer, parse_customer_node
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.manifest import write_manifest
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import STATE_SUBDIR, load_state, write_dataset, write_state


def live(**kw) -> LiveCustomer:
    base = dict(
        id="lin-1",
        name="Globex",
        domains=["globex.example.test"],
        revenue=250000,
        tier="Tier 1",
        status="Active",
        external_ids=[],
        updated_at="2026-10-07T00:00:00.000Z",
    )
    base.update(kw)
    return LiveCustomer(**base)


def test_match_by_external_id_updates_and_refreshes_linear_synced():
    existing = [
        LinearCustomer(
            name="Globex Old",
            domains=["globex.example.test"],
            external_ids=["globex"],
            linear_synced=LinearSynced("stale", "old", "oldhash"),
        )
    ]
    [out] = reconcile_inbound([live(external_ids=["globex"])], existing)
    assert out.name == "Globex"  # mirror reflects LIVE
    assert out.external_ids == ["globex"]  # our slug preserved
    assert out.linear_synced.linear_id == "lin-1"
    assert out.linear_synced.updated_at == "2026-10-07T00:00:00.000Z"
    assert out.linear_synced.content_hash == content_hash_of(out)


def test_no_id_recovered_by_domain_or_name():
    existing = [
        LinearCustomer(
            name="Hooli Casinos",
            domains=["hoolicasinos.example.test"],
            external_ids=["hoolicasinos"],
        )
    ]
    out = reconcile_inbound(
        [
            live(id="l1", name="Hoolis", domains=[], revenue=None, tier=None, external_ids=[]),
            live(
                id="l2",
                name="Hooli Casinos",
                domains=["hoolicasinos.example.test"],
                revenue=None,
                tier=None,
            ),
        ],
        existing,
    )
    by_name = {o.name: o for o in out}
    # the mirror holds what Linear holds: neither live customer carries the slug, so
    # neither gets it from the old row's domain or name (final finding 1)
    assert by_name["Hooli Casinos"].external_ids == []
    assert by_name["Hoolis"].external_ids == []


def test_unmatched_live_emitted_as_new_mirror_entry():
    out = reconcile_inbound(
        [live(id="new", name="New Co", domains=["new.example.test"], external_ids=[])], []
    )
    assert len(out) == 1
    assert out[0].name == "New Co"
    assert out[0].linear_synced.linear_id == "new"


def test_idempotent_on_rerun():
    live_list = [
        live(external_ids=["globex"]),
        live(id="l2", name="Beta", domains=["beta.example.test"]),
    ]
    out1 = reconcile_inbound(live_list, [])
    out2 = reconcile_inbound(live_list, out1)
    assert out1 == out2


def _node(lid, name, *, domains=(), status="Active", xids=(), updated="2026-10-01T00:00:00.000Z"):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": None,
            "externalIds": list(xids),
            "updatedAt": updated,
            "status": {"id": "s", "name": status} if status else None,
            "tier": None,
        }
    )


def test_sync_inbound_never_copies_external_ids_to_a_duplicate(tmp_path):
    """A second Linear customer sharing a stamped one's domain is mirrored as it is in
    Linear (no canonical id), so `check` reports it as a known duplicate and passes."""
    a = _node("A", "Acme [Active]", domains=["acme.example.test"], xids=["acme"])
    b = _node("B", "Acme", domains=["acme.example.test"], updated="2026-10-02T00:00:00.000Z")
    state = reconcile_inbound([a, b], reconcile_inbound([a], []))
    by_id = {s.linear_synced.linear_id: s for s in state}
    assert by_id["A"].external_ids == ["acme"]
    assert by_id["B"].external_ids == []

    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    store.add_identity(
        "acme",
        names=["Acme"],
        domains=["acme.example.test"],
        sources={"notion": "acme", "linear": "A"},
    )
    store.save()
    table = load_identities(tmp_path / IDENTITIES_FILE)
    desired = generate_linear(
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real"
            )
        ]
    )
    res = run_check(desired, state, [a, b], table, entries=None)
    assert not res.drift.any()
    assert [(cid, lv.id) for cid, lv in res.plan.known_duplicates] == [("acme", "B")]
    assert res.code == 0


def test_mirror_row_takes_every_field_from_live_only():
    """An old state row sharing the live customer's domain and name contributes nothing."""
    existing = reconcile_inbound(
        [_node("old", "Beta", domains=["beta.example.org"], xids=["beta"])], []
    )
    [out] = reconcile_inbound([_node("new", "Beta", domains=["beta.example.org"])], existing)
    assert out.external_ids == []
    assert out.linear_synced.linear_id == "new"


def test_empty_linear_status_is_not_mirrored_as_active(tmp_path, run_cli):
    """D9/R12: a Linear customer with no status is mirrored with no status, its stub
    gets `classification: null`, `check` fails naming it, and the mirror is not drift."""
    lv = _node("lin_n", "Nostatus Co", domains=["nostatus.example.org"], status=None)
    assert lv.status == ""
    [row] = reconcile_inbound([lv], [])
    assert row.status is None
    assert not detect_drift([row], [lv]).any()

    (tmp_path / "datasets.yaml").write_text(
        "datasets: [notion, linear-found]\n"
        "linear_status_classification: {Active: real, Prospect: prospect, Churned: churned}\n"
    )
    write_dataset([], tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_state([row], tmp_path)
    write_manifest(tmp_path / STATE_SUBDIR, "sync-inbound")
    assert [r.status for r in load_state(tmp_path)] == [None]
    assert (
        run_cli(["ingest", "--only", "linear-found", "--out-dir", str(tmp_path), "--no-input"]) == 0
    )
    [stub] = load_datasets(tmp_path, DatasetConfig(names=[LINEAR_FOUND]))
    assert stub.record.classification is None
    assert stub.record.status is None
    assert any(
        e.startswith("unmapped Linear status: 'Nostatus Co' (lin_n)")
        for e in offline_errors(tmp_path)
    )
