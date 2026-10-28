"""`ingest` builds the `linear-found` dataset and appends identities."""

import shutil
from datetime import date

from gitops_engine.datasets import dataset_dir
from gitops_engine.ignore import BUILTIN_PREFIX, load_ignore, partition_live
from gitops_engine.manifest import MANIFEST, write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync.datasets import (
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    load_datasets,
    load_datasets_config,
)
from customer_registry_sync.deploy import plan_deploy
from customer_registry_sync.generate import generate_from_dir
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_files import IGNORE_FILE, IGNORE_FORMAT
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_found import (
    STUB_NOTE,
    append_record_identities,
    build_linear_found,
    status_errors,
)
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import STATE_SUBDIR, load_state, write_dataset, write_state

DATASETS_YAML = """\
datasets: [notion, linear-found]
linear_status_classification:
  Active: real
  Prospect: prospect
  Churned: churned
"""
BOTH = DatasetConfig(names=[NOTION, LINEAR_FOUND])
DAY1, DAY2 = date(2026, 10, 1), date(2026, 10, 20)


def _node(lid, name, *, domains=(), revenue=None, status="Active", tier=None, xids=()):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": revenue,
            "externalIds": list(xids),
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s", "name": status} if status else None,
            "tier": {"id": "t", "name": tier} if tier else None,
        }
    )


def _tree(tmp_path, *, records=(), nodes=()):
    """A data dir from the real producers: datasets.yaml, a notion dataset, a mirror."""
    (tmp_path / "datasets.yaml").write_text(DATASETS_YAML)
    write_dataset(list(records), tmp_path, dataset=NOTION)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    _mirror(tmp_path, *nodes)


def _mirror(tmp_path, *nodes):
    write_state(reconcile_mirror(list(nodes), LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / STATE_SUBDIR, "sync-inbound")


def _managed(tmp_path):
    ignore = load_ignore(tmp_path / IGNORE_FILE, IGNORE_FORMAT)
    live = [
        LINEAR_SCHEMA.state_to_live(r) for r in load_state(tmp_path) if r.linear_synced is not None
    ]
    return partition_live(live, ignore, LINEAR_SCHEMA, scope=None)[0]


def _ingest(run_cli, tmp_path):
    return run_cli(["ingest", "--only", "linear-found", "--out-dir", str(tmp_path), "--no-input"])


def _stubs(tmp_path):
    return {
        e.source_id: e.record for e in load_datasets(tmp_path, BOTH) if e.dataset == LINEAR_FOUND
    }


def _identities(tmp_path):
    return load_identities(tmp_path / IDENTITIES_FILE).entries


def _build(tmp_path, today=DAY1):
    """`build_linear_found` over the tree's mirror, as `ingest` calls it (no ignore file)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    entries = load_datasets(tmp_path, BOTH)
    append_record_identities(store, entries)
    managed = sorted(_managed(tmp_path), key=lambda lv: lv.id)
    return store, build_linear_found(
        managed, store, entries, load_datasets_config(tmp_path), today=today
    )


ACME = CustomerRecord(canonical_name="Acme", domains=["acme.example.test"], classification="real")


def test_stubs_only_customers_without_notion_identity(tmp_path, run_cli):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_a", "Acme [Active]", domains=["acme.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"], revenue=5000, tier="Tier 2"),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    stubs = _stubs(tmp_path)
    assert list(stubs) == ["beta"]
    beta = stubs["beta"]
    assert beta.canonical_name == "Beta Corp"
    assert beta.domains == ["beta.example.test"]
    assert beta.arr == 5000.0
    assert beta.tier == "Tier 2"
    assert beta.status == "Active"
    assert beta.classification == "real"
    assert beta.linear_customer_id == "lin_b"
    assert beta.sources == ["linear:lin_b"]
    ids = _identities(tmp_path)
    assert ids["beta"].sources == {"linear": "lin_b"}
    assert ids["acme"].sources == {"notion": "acme", "linear": "lin_a"}
    assert (dataset_dir(tmp_path, LINEAR_FOUND) / MANIFEST).is_file()


def test_canonical_id_kept_across_runs_and_rebuild_from_empty_dir(tmp_path, run_cli):
    _tree(
        tmp_path,
        nodes=[
            _node("lin_b", "Beta Corp", domains=["beta.example.test"]),
            _node("lin_c", "Gamma", domains=["beta.other.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    first = sorted(_stubs(tmp_path))
    assert first == ["beta", "beta-2"]
    assert _ingest(run_cli, tmp_path) == 0
    assert sorted(_stubs(tmp_path)) == first
    shutil.rmtree(dataset_dir(tmp_path, LINEAR_FOUND))
    assert _ingest(run_cli, tmp_path) == 0
    stubs = _stubs(tmp_path)
    assert {s: r.linear_customer_id for s, r in stubs.items()} == {
        "beta": "lin_b",
        "beta-2": "lin_c",
    }


def test_stub_keeps_canonical_id_when_its_domain_changes(tmp_path, run_cli):
    _tree(tmp_path, nodes=[_node("lin_b", "Beta Corp", domains=["beta.example.test"])])
    assert _ingest(run_cli, tmp_path) == 0
    # Linear owns the stub: its domain and name change there
    _mirror(
        tmp_path,
        _node("lin_b", "Beta Holdings", domains=["newbeta.example.test"], xids=["beta"]),
    )
    assert _ingest(run_cli, tmp_path) == 0
    stubs = _stubs(tmp_path)
    assert list(stubs) == ["beta"]
    assert stubs["beta"].canonical_name == "Beta Holdings"
    assert [r.external_ids for r in generate_from_dir(tmp_path).rows] == [["beta"]]
    # keyed by Linear id: with the identity table gone, the stub still keeps its stem
    (tmp_path / IDENTITIES_FILE).unlink()
    assert _ingest(run_cli, tmp_path) == 0
    assert list(_stubs(tmp_path)) == ["beta"]
    assert list(_identities(tmp_path)) == ["beta"]


def test_generate_uses_stub_stem_when_stub_has_no_identity(tmp_path, run_cli):
    _tree(tmp_path, nodes=[_node("lin_b", "Beta Corp", domains=["beta.example.test"])])
    assert _ingest(run_cli, tmp_path) == 0
    (tmp_path / IDENTITIES_FILE).unlink()
    _mirror(tmp_path, _node("lin_b", "Beta Corp", domains=["newbeta.example.test"]))
    # rebuild the stub from a later mirror (its domain changed) with no identity table
    _, build = _build(tmp_path)  # store not saved: identities.yaml stays absent
    assert [s for s, _ in build.stubs] == ["beta"]
    write_dataset(
        [r for _, r in build.stubs],
        tmp_path,
        dataset=LINEAR_FOUND,
        stems=[s for s, _ in build.stubs],
    )
    assert [r.external_ids for r in generate_from_dir(tmp_path).rows] == [["beta"]]


def test_status_suffix_stripped(tmp_path, run_cli):
    _tree(tmp_path, nodes=[_node("lin_p", "Pilot Co [Prospect]", status="Prospect")])
    assert _ingest(run_cli, tmp_path) == 0
    [(stem, rec)] = _stubs(tmp_path).items()
    assert stem == "pilot-co"
    assert rec.canonical_name == "Pilot Co"
    assert rec.classification == "prospect"
    assert _identities(tmp_path)["pilot-co"].values["names"] == ["Pilot Co"]


def test_first_sighting_date_kept(tmp_path):
    _tree(tmp_path, nodes=[_node("lin_b", "Beta Corp", domains=["beta.example.test"])])
    store, build = _build(tmp_path, today=DAY1)
    store.save()
    write_dataset(
        [r for _, r in build.stubs],
        tmp_path,
        dataset=LINEAR_FOUND,
        stems=[s for s, _ in build.stubs],
    )
    first_note = STUB_NOTE.format(date="2026-10-01")
    assert build.stubs[0][1].notes == first_note
    _, later = _build(tmp_path, today=DAY2)
    assert later.stubs[0][1].notes == first_note


def test_stub_with_no_domains(tmp_path, run_cli):
    _tree(tmp_path, nodes=[_node("lin_r", "Vandelay Global Services")])
    assert _ingest(run_cli, tmp_path) == 0
    stubs = _stubs(tmp_path)
    assert list(stubs) == ["vandelay-global-services"]
    assert stubs["vandelay-global-services"].domains == []
    text = (dataset_dir(tmp_path, LINEAR_FOUND) / "vandelay-global-services.yaml").read_text()
    assert "domains: []" in text


def test_name_only_and_alias_name_only_match_create_no_stub(tmp_path, run_cli):
    dunder = CustomerRecord(
        canonical_name="Dunder Technologies",
        domains=["dunder.example.test"],
        classification="real",
    )
    _tree(
        tmp_path,
        records=[ACME, dunder],
        nodes=[
            _node("lin_a", "Acme [Active]"),  # name only, no domain
            _node("lin_t", "Dunder", domains=["dunder-other.example.test"]),  # alias name
        ],
    )
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    append_record_identities(store, load_datasets(tmp_path, BOTH))
    store.add_values("dunder", names=["Dunder"])
    store.save()
    assert _ingest(run_cli, tmp_path) == 0
    assert _stubs(tmp_path) == {}
    ids = _identities(tmp_path)
    assert sorted(ids) == ["acme", "dunder"]
    assert ids["dunder"].sources["linear"] == "lin_t"


def test_linear_only_domain_appended_to_identity(tmp_path, run_cli):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_a", "Acme [Active]", domains=["acme.example.test", "Acme-Alt.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert _identities(tmp_path)["acme"].values["domains"] == [
        "acme.example.test",
        "Acme-Alt.example.test",
    ]
    # a stub's later Linear-only domain lands on its identity too
    _mirror(
        tmp_path,
        _node("lin_a", "Acme [Active]", domains=["acme.example.test", "Acme-Alt.example.test"]),
        _node("lin_b", "Beta Corp", domains=["beta.example.test", "beta-two.example.test"]),
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert _identities(tmp_path)["beta"].values["domains"] == [
        "beta.example.test",
        "beta-two.example.test",
    ]


def test_ignored_customer_not_stubbed_and_gets_no_identity(tmp_path, run_cli):
    _tree(
        tmp_path,
        nodes=[
            _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"]),
            _node("lin_z", f"{BUILTIN_PREFIX}alpha", domains=["zzz.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"]),
        ],
    )
    (tmp_path / IGNORE_FILE).write_text(
        "customers:\n  - linear_id: lin_own\n    name: Pied Piper\n    reason: our own company\n"
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert list(_stubs(tmp_path)) == ["beta"]
    assert list(_identities(tmp_path)) == ["beta"]


def test_same_name_live_customers_make_one_stub(tmp_path, capsys, run_cli):
    _tree(
        tmp_path,
        nodes=[
            _node("lin_2", "Dup Co", domains=["dup2.example.test"]),
            _node("lin_1", "Dup Co [Active]", domains=["dup.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    stubs = _stubs(tmp_path)
    assert list(stubs) == ["dup"]
    assert stubs["dup"].linear_customer_id == "lin_1"
    ident = _identities(tmp_path)["dup"]
    assert ident.values["domains"] == ["dup.example.test"]  # the duplicate contributes nothing
    # neither carries an id, so the lowest Linear id must not settle the pick
    assert ident.sources == {}
    out = capsys.readouterr().out
    assert "dup: no customer carries the canonical or recorded Linear id" in out
    assert "'Dup Co' (lin_2)" in out


def test_linear_found_ingest_is_idempotent(tmp_path, run_cli):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_a", "Acme [Active]", domains=["acme.example.test", "acme-two.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"], revenue=10),
            _node("lin_r", "Vandelay"),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    lf = dataset_dir(tmp_path, LINEAR_FOUND)

    def snapshot():
        files = {p.name: p.read_bytes() for p in lf.glob("*.yaml") if p.name != MANIFEST}
        return (tmp_path / IDENTITIES_FILE).read_bytes(), files

    before = snapshot()
    assert _ingest(run_cli, tmp_path) == 0
    assert snapshot() == before
    assert set(before[1]) == {"beta.yaml", "vandelay.yaml"}


def test_linear_found_refuses_without_mirror(tmp_path, capsys, run_cli):
    _tree(tmp_path, nodes=[_node("lin_b", "Beta Corp", domains=["beta.example.test"])])
    assert _ingest(run_cli, tmp_path) == 0
    stub = dataset_dir(tmp_path, LINEAR_FOUND) / "beta.yaml"
    stub_bytes = stub.read_bytes()
    ids_bytes = (tmp_path / IDENTITIES_FILE).read_bytes()
    shutil.rmtree(tmp_path / STATE_SUBDIR)
    assert _ingest(run_cli, tmp_path) == 2
    assert "run sync-inbound first" in capsys.readouterr().err
    assert stub.read_bytes() == stub_bytes
    assert (tmp_path / IDENTITIES_FILE).read_bytes() == ids_bytes


def test_ambiguous_duplicates_record_no_linear_source(tmp_path, capsys, run_cli):
    """Two unstamped customers on a Notion identity with no recorded Linear id: the build
    records nothing on the identity, reports it, and the planner's error still fails
    after the build saves."""
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_1", "Acme [Active]", domains=["acme.example.test"]),
            _node("lin_2", "Acme Inc", domains=["acme.example.test", "acme-two.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    ident = _identities(tmp_path)["acme"]
    assert ident.sources == {"notion": "acme"}
    assert ident.values["domains"] == ["acme.example.test"]
    assert _stubs(tmp_path) == {}
    out = capsys.readouterr().out
    assert "acme: no customer carries the canonical or recorded Linear id" in out
    plan = plan_deploy(
        generate_from_dir(tmp_path).rows,
        [LINEAR_SCHEMA.state_to_live(r) for r in load_state(tmp_path)],
        load_identities(tmp_path / IDENTITIES_FILE),
    )
    assert any(
        e.startswith("known duplicates for acme with no canonical or recorded Linear id")
        for e in plan.errors
    )


def test_new_identity_for_two_unstamped_duplicates_records_no_linear_source(
    tmp_path, capsys, run_cli
):
    """Two unstamped customers that resolve to nothing: the new identity records no
    Linear id (the lowest id must not settle the pick), the build reports it, and the
    planner's error stands."""
    _tree(
        tmp_path,
        nodes=[
            _node("lin_1", "Gamma", domains=["gamma.example.test"]),
            _node("lin_2", "Gamma Inc", domains=["gamma.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    ident = _identities(tmp_path)["gamma"]
    assert ident.sources == {}
    out = capsys.readouterr().out
    assert "gamma: no customer carries the canonical or recorded Linear id" in out
    plan = plan_deploy(
        generate_from_dir(tmp_path).rows,
        [LINEAR_SCHEMA.state_to_live(r) for r in load_state(tmp_path)],
        load_identities(tmp_path / IDENTITIES_FILE),
    )
    assert any(
        e.startswith("known duplicates for gamma with no canonical or recorded Linear id")
        for e in plan.errors
    )


def test_status_errors_name_unmapped_and_unclassified_stubs(tmp_path):
    cfg = DatasetConfig(
        names=[NOTION, LINEAR_FOUND],
        status_classification={"Active": "real"},
    )
    base = {"domains": [], "linear_customer_id": "lin_s", "sources": ["linear:lin_s"]}
    ok = CustomerRecord(canonical_name="Fine", status="Active", classification="real", **base)
    unclassified = CustomerRecord(canonical_name="Blank", status="Active", **base)
    unmapped = CustomerRecord(canonical_name="Odd", status="Paused", **base)
    notion = CustomerRecord(canonical_name="Skipped", status="Paused", domains=[])
    write_dataset([ok, unclassified, unmapped], tmp_path, dataset=LINEAR_FOUND)
    write_dataset([notion], tmp_path)
    errors = status_errors(load_datasets(tmp_path, BOTH), cfg)
    assert len(errors) == 2
    assert errors[0].startswith("stub 'Blank' has no classification")
    assert errors[1].startswith("unmapped Linear status: 'Odd' (lin_s) has status 'Paused'.")
    assert "linear_status_classification in datasets.yaml" in errors[1]
    assert "ignore --linear-id lin_s" in errors[1]


def test_two_customers_stamped_with_the_canonical_id_are_reported(tmp_path, capsys, run_cli):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_1", "Acme [Active]", domains=["acme.example.test"], xids=["acme"]),
            _node("lin_2", "Acme Inc", domains=["acme.example.test"], xids=["acme"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert _identities(tmp_path)["acme"].sources == {"notion": "acme"}
    out = capsys.readouterr().out
    assert "two or more Linear customers carry the canonical id acme" in out
    assert "nothing recorded" in out


def test_recorded_linear_id_picks_the_primary_and_the_other_is_a_known_duplicate(tmp_path):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_1", "Acme Inc", domains=["acme.example.test"]),
            _node("lin_2", "Acme [Active]", domains=["acme.example.test", "acme-two.example.test"]),
        ],
    )
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    append_record_identities(store, load_datasets(tmp_path, BOTH))
    store.add_values("acme", sources={"linear": "lin_2"})
    store.save()
    store, build = _build(tmp_path)
    assert build.stubs == []
    assert build.reports == []
    assert build.duplicates == ["acme: 'Acme Inc' (lin_1) is a known duplicate; merge it in Linear"]
    # only the primary contributes its Linear-only domain
    assert store.table.entries["acme"].values["domains"] == [
        "acme.example.test",
        "acme-two.example.test",
    ]
