import pytest
from gitops_engine.datasets import dataset_dir
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync.check import detect_drift, offline_errors, run_check
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetConfig, load_datasets
from customer_registry_sync.generate import generate_linear
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_client import LiveCustomer, parse_customer_node
from customer_registry_sync.linear_schema import (
    LINEAR_SCHEMA,
    LinearCustomer,
    LinearSynced,
    content_hash_of,
)
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


class FakeClient:
    def __init__(self, live):
        self._live = live

    def list_customers(self):
        return list(self._live)


def _sync_inbound(tmp_path, monkeypatch, run_cli, live_rows):
    """Run `sync-inbound` against `live_rows`; the mirror then holds what it wrote."""
    from customer_registry_sync import cli

    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient(live_rows))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path)]) == 0
    return load_state(tmp_path)


def test_match_by_external_id_updates_and_refreshes_linear_synced(tmp_path, monkeypatch, run_cli):
    # an old state row for the same customer, with a stale mark and an old name
    write_state(
        [
            LinearCustomer(
                name="Globex Old",
                domains=["globex.example.test"],
                external_ids=["globex"],
                linear_synced=LinearSynced("stale", "old", "oldhash"),
            )
        ],
        tmp_path,
    )
    [out] = _sync_inbound(tmp_path, monkeypatch, run_cli, [live(external_ids=["globex"])])
    assert out.name == "Globex"  # mirror reflects LIVE
    assert out.external_ids == ["globex"]  # the id Linear holds
    assert out.linear_synced.linear_id == "lin-1"
    assert out.linear_synced.updated_at == "2026-10-07T00:00:00.000Z"
    assert out.linear_synced.content_hash == content_hash_of(out)


def test_no_id_recovered_by_domain_or_name(tmp_path, monkeypatch, run_cli):
    # an old state row carries a canonical id and shares a domain and a name with live rows
    write_state(
        [
            LinearCustomer(
                name="Hooli Casinos",
                domains=["hoolicasinos.example.test"],
                external_ids=["hoolicasinos"],
            )
        ],
        tmp_path,
    )
    out = _sync_inbound(
        tmp_path,
        monkeypatch,
        run_cli,
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
    )
    by_name = {o.name: o for o in out}
    # the mirror holds what Linear holds: neither live customer carries the slug, so
    # neither gets it from the old row's domain or name (final finding 1)
    assert by_name["Hooli Casinos"].external_ids == []
    assert by_name["Hoolis"].external_ids == []


def test_unmatched_live_emitted_as_new_mirror_entry():
    out = reconcile_mirror(
        [live(id="new", name="New Co", domains=["new.example.test"], external_ids=[])],
        LINEAR_SCHEMA,
    )
    assert len(out) == 1
    assert out[0].name == "New Co"
    assert out[0].linear_synced.linear_id == "new"


def test_idempotent_on_rerun():
    live_list = [
        live(external_ids=["globex"]),
        live(id="l2", name="Beta", domains=["beta.example.test"]),
    ]
    out1 = reconcile_mirror(live_list, LINEAR_SCHEMA)
    out2 = reconcile_mirror(live_list, LINEAR_SCHEMA)
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
    state = reconcile_mirror([a, b], LINEAR_SCHEMA)
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


def test_mirror_row_takes_every_field_from_live_only(tmp_path, monkeypatch, run_cli):
    """An old state row sharing the live customer's domain and name contributes nothing."""
    old = _node("old", "Beta", domains=["beta.example.org"], xids=["beta"])
    [first] = _sync_inbound(tmp_path, monkeypatch, run_cli, [old])
    assert first.external_ids == ["beta"]
    [out] = _sync_inbound(
        tmp_path, monkeypatch, run_cli, [_node("new", "Beta", domains=["beta.example.org"])]
    )
    assert out.external_ids == []
    assert out.linear_synced.linear_id == "new"


def test_empty_linear_status_is_not_mirrored_as_active(tmp_path, run_cli):
    """A Linear customer with no status is mirrored with no status, its stub
    gets `classification: null`, `check` fails naming it, and the mirror is not drift."""
    lv = _node("lin_n", "Nostatus Co", domains=["nostatus.example.org"], status=None)
    assert lv.status == ""
    [row] = reconcile_mirror([lv], LINEAR_SCHEMA)
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


# ---- Linear edits recorded in linear-overrides.yaml ----

_DATASETS_YAML = """\
datasets: [notion, linear-found]
linear_status_classification:
  Active: real
  Prospect: prospect
  Churned: churned
"""


def _acme(**kw) -> CustomerRecord:
    base = {
        "canonical_name": "Acme",
        "domains": ["acme.example.test", "alt.example.test"],
        "classification": "real",
        "arr": 9.0,
        "tier": "Tier 1",
    }
    base.update(kw)
    return CustomerRecord(**base)


def _mirror(tmp_path, rows) -> None:
    """The state mirror as `sync-inbound` writes it."""
    write_state(reconcile_mirror(list(rows), LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / STATE_SUBDIR, "sync-inbound")


def _offline_refresh(tmp_path, run_cli) -> None:
    """The offline refresh steps: `ingest --only linear-found`, then `generate`."""
    args = ["--out-dir", str(tmp_path), "--no-input"]
    assert run_cli(["ingest", "--only", "linear-found", *args]) == 0
    assert run_cli(["generate", *args]) == 0


def _live_of(d: LinearCustomer, lid: str) -> LiveCustomer:
    """The live customer a deploy of `d` leaves in Linear."""
    return LiveCustomer(
        id=lid,
        name=d.name,
        domains=list(d.domains),
        revenue=d.revenue,
        tier=d.tier,
        status=d.status,
        external_ids=list(d.external_ids),
        updated_at="2026-10-01T00:00:00.000Z",
    )


def _in_sync(tmp_path, run_cli, records=None, *, extra_live=()) -> LiveCustomer:
    """A data dir from the real producers with Linear equal to the registry: the
    dataset, identities, `generate`, and a mirror of the deployed customer (plus
    `extra_live`, Linear-only customers that become stubs). Returns Acme's live row."""
    from customer_registry_sync.writers import load_desired

    (tmp_path / "datasets.yaml").write_text(_DATASETS_YAML)
    write_dataset(list(records or [_acme()]), tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    _mirror(tmp_path, [])
    _offline_refresh(tmp_path, run_cli)
    [d] = [r for r in load_desired(tmp_path) if r.external_ids == ["acme"]]
    lv = _live_of(d, "lin_acme")
    _mirror(tmp_path, [lv, *extra_live])
    _offline_refresh(tmp_path, run_cli)
    return lv


def _sync_with(tmp_path, monkeypatch, run_cli, live_rows, *extra) -> int:
    from customer_registry_sync import cli

    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient(live_rows))
    return run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--no-input", *extra])


def _check(tmp_path, monkeypatch, run_cli, live_rows) -> int:
    from customer_registry_sync import cli

    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient(live_rows))
    return run_cli(["check", "--out-dir", str(tmp_path)])


def _overrides(tmp_path) -> dict:
    """The `customers` entries of linear-overrides.yaml (none when the file is absent)."""
    import yaml

    p = tmp_path / "linear-overrides.yaml"
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text()) or {}).get("customers") or {}


def test_revenue_cleared_in_linear_is_absorbed(tmp_path, monkeypatch, capsys, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, revenue=None)
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert "sync-inbound: 1 mirrored; 1 absorbed, 0 pending" in capsys.readouterr().out
    assert _overrides(tmp_path) == {"acme": {"pin": {"revenue": None}}}
    _offline_refresh(tmp_path, run_cli)
    assert _check(tmp_path, monkeypatch, run_cli, [edited]) == 0


def test_domain_removed_in_linear_is_absorbed(tmp_path, monkeypatch, run_cli):
    from dataclasses import replace

    from customer_registry_sync.deploy_plan import offline_plan

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, domains=["acme.example.test"])
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {"acme": {"domains": {"remove": ["alt.example.test"]}}}
    _offline_refresh(tmp_path, run_cli)
    plan, code = offline_plan(tmp_path, live=[edited])
    assert (plan.creates, plan.updates, plan.aborts, code) == ([], [], [], 0)


def test_tier_changed_in_linear_is_pending(tmp_path, monkeypatch, capsys, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, tier="Tier 2")
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert "sync-inbound: 1 mirrored; 0 absorbed, 1 pending" in capsys.readouterr().out
    assert _overrides(tmp_path) == {"acme": {"pending": {"tier": "Tier 2"}}}
    _offline_refresh(tmp_path, run_cli)
    assert _check(tmp_path, monkeypatch, run_cli, [edited]) == 3


def test_domain_added_in_linear_is_pending_not_learned(tmp_path, monkeypatch, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, domains=[*lv.domains, "acme.dev"])
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {"acme": {"pending": {"domains_add": ["acme.dev"]}}}
    _offline_refresh(tmp_path, run_cli)
    table = load_identities(tmp_path / IDENTITIES_FILE)
    assert "acme.dev" not in table.entries["acme"].values["domains"]


@pytest.mark.parametrize("renamed", ["Acme [Churned]", "Acme"])
def test_name_suffix_only_change_is_not_an_edit(tmp_path, monkeypatch, run_cli, renamed):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    assert lv.name == "Acme [Active]"
    assert _sync_with(tmp_path, monkeypatch, run_cli, [replace(lv, name=renamed)]) == 0
    assert _overrides(tmp_path) == {}
    assert not (tmp_path / "linear-overrides.yaml").exists()


def test_name_change_pending_without_suffix(tmp_path, monkeypatch, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, name="Acme Inc [Active]")
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {"acme": {"pending": {"name": "Acme Inc"}}}


def test_unmerged_deploy_write_is_not_an_edit(tmp_path, monkeypatch, run_cli):
    """Review Focus 1: the mirror predates a deploy whose state PR is unmerged, so it
    differs from live; live equals desired, so nothing is an edit."""
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli, [_acme(arr=9.0)])
    _mirror(tmp_path, [replace(lv, revenue=5)])  # the mirror before the deploy wrote 9
    assert _sync_with(tmp_path, monkeypatch, run_cli, [lv]) == 0
    assert _overrides(tmp_path) == {}


def test_stale_mirror_registry_change_records_conservative_pending(tmp_path, monkeypatch, run_cli):
    """Mirror 5 (stale), live 7 (a deploy whose state PR is unmerged), desired 9 (a later
    registry change): one pending entry with Linear's value, and no pin."""
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli, [_acme(arr=9.0)])
    _mirror(tmp_path, [replace(lv, revenue=5)])
    assert _sync_with(tmp_path, monkeypatch, run_cli, [replace(lv, revenue=7)]) == 0
    assert _overrides(tmp_path) == {"acme": {"pending": {"revenue": 7}}}


def test_pending_dropped_when_live_returns_to_desired(tmp_path, monkeypatch, capsys, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, tier="Tier 2")
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {"acme": {"pending": {"tier": "Tier 2"}}}
    _offline_refresh(tmp_path, run_cli)
    # Linear is put back to the registry's value: the pending decision is moot
    assert _sync_with(tmp_path, monkeypatch, run_cli, [lv]) == 0
    assert _overrides(tmp_path) == {}
    _offline_refresh(tmp_path, run_cli)
    assert _check(tmp_path, monkeypatch, run_cli, [lv]) == 0


def test_pending_domain_add_dropped_when_the_registry_carries_it(tmp_path, monkeypatch, run_cli):
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, domains=[*lv.domains, "acme.dev"])
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {"acme": {"pending": {"domains_add": ["acme.dev"]}}}
    # upstream now carries the domain (re-cased: compared by key)
    write_dataset([_acme(domains=["acme.example.test", "alt.example.test", "Acme.dev"])], tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    _offline_refresh(tmp_path, run_cli)
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert _overrides(tmp_path) == {}


def test_stub_edit_is_not_recorded(tmp_path, monkeypatch, run_cli):
    from dataclasses import replace

    beta = live(id="lin_beta", name="Beta", domains=["beta.example.test"], revenue=3, tier=None)
    lv = _in_sync(tmp_path, run_cli, extra_live=[beta])
    stubs = load_datasets(tmp_path, DatasetConfig(names=[LINEAR_FOUND]))
    assert [s.record.canonical_name for s in stubs] == ["Beta"]
    edited = replace(beta, revenue=None, tier="Tier 2", name="Beta Two")
    assert _sync_with(tmp_path, monkeypatch, run_cli, [lv, edited]) == 0
    assert _overrides(tmp_path) == {}


def test_sync_inbound_twice_leaves_the_overrides_file_byte_identical(
    tmp_path, monkeypatch, run_cli
):
    """The second run over the same live data finds the mirror caught up: no edit."""
    from dataclasses import replace

    lv = _in_sync(tmp_path, run_cli)
    edited = replace(lv, revenue=None, tier="Tier 2", domains=["acme.example.test", "acme.dev"])
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    first = (tmp_path / "linear-overrides.yaml").read_bytes()
    assert _overrides(tmp_path) == {
        "acme": {
            "pin": {"revenue": None},
            "domains": {"remove": ["alt.example.test"]},
            "pending": {"tier": "Tier 2", "domains_add": ["acme.dev"]},
        }
    }
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert (tmp_path / "linear-overrides.yaml").read_bytes() == first
    _offline_refresh(tmp_path, run_cli)
    assert _sync_with(tmp_path, monkeypatch, run_cli, [edited]) == 0
    assert (tmp_path / "linear-overrides.yaml").read_bytes() == first
