"""`ingest` builds the `linear-found` dataset and appends identities."""

import shutil
from datetime import date

import pytest

from gitops_engine.datasets import dataset_dir
from gitops_engine.ignore import BUILTIN_PREFIX
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
from customer_registry_sync.linear_files import no_hidden, partition_hidden
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


def _managed(tmp_path, table):
    live = [
        LINEAR_SCHEMA.state_to_live(r) for r in load_state(tmp_path) if r.linear_synced is not None
    ]
    return partition_hidden(live, no_hidden(), table, scope=None)[0]


def _ingest(run_cli, tmp_path):
    return run_cli(["ingest", "--only", "linear-found", "--out-dir", str(tmp_path), "--no-input"])


def _stubs(tmp_path):
    return {
        e.source_id: e.record for e in load_datasets(tmp_path, BOTH) if e.dataset == LINEAR_FOUND
    }


def _identities(tmp_path):
    return load_identities(tmp_path / IDENTITIES_FILE).entries


def _build(tmp_path, today=DAY1):
    """`build_linear_found` over the tree's mirror, as `ingest` calls it (no overrides file)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    entries = load_datasets(tmp_path, BOTH)
    append_record_identities(store, entries)
    managed = sorted(_managed(tmp_path, store.table), key=lambda lv: lv.id)
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


def test_linear_only_domain_appended_to_stub_identity_only(tmp_path, run_cli):
    _tree(
        tmp_path,
        records=[ACME],
        nodes=[
            _node("lin_a", "Acme [Active]", domains=["acme.example.test", "Acme-Alt.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"]),
        ],
    )
    assert _ingest(run_cli, tmp_path) == 0
    # a customer with a notion record learns only its Linear id: a domain added in
    # Linear reaches the registry through linear-overrides.yaml, never the identity
    acme = _identities(tmp_path)["acme"]
    assert acme.values["domains"] == ["acme.example.test"]
    assert acme.sources["linear"] == "lin_a"
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


def test_hidden_customer_not_stubbed(tmp_path, run_cli):
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    _tree(
        tmp_path,
        nodes=[
            _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"]),
            _node("lin_z", f"{BUILTIN_PREFIX}alpha", domains=["zzz.example.test"]),
            _node("lin_s", "sandbox-lab", domains=["lab.example.test"]),
            _node("lin_b", "Beta Corp", domains=["beta.example.test"]),
        ],
    )
    # hidden by id: its identity (which `hide` appends) carries the Linear id
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    store.add_identity("own", names=["Pied Piper"], domains=[], sources={"linear": "lin_own"})
    store.save()
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n  own:\n    hidden: {reason: our own company}\n"
        "hidden_name_prefixes:\n  - {prefix: sandbox-, reason: manual experiments}\n"
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert list(_stubs(tmp_path)) == ["beta"]
    # prefix-hidden customers get no identity either; the hidden one keeps its own
    assert list(_identities(tmp_path)) == ["own", "beta"]


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
    assert "hide --linear-id lin_s" in errors[1]


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
    # a customer with a notion record learns no Linear-only domain, from either row
    assert store.table.entries["acme"].values["domains"] == ["acme.example.test"]
    assert store.table.entries["acme"].sources["linear"] == "lin_2"


# --- a stub handing over to Notion -------------------------------------------------

BETA_NODE = _node("lin_b", "Beta Corp", domains=["a.test", "b.test"], revenue=5000, tier="Gold")
BETA = CustomerRecord(canonical_name="Beta Corp", domains=["a.test"], classification="real")


def _notion(tmp_path, *records):
    write_dataset(list(records), tmp_path, dataset=NOTION)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")


def _overrides(tmp_path):
    from customer_registry_sync.linear_overrides import load_overrides_file

    return load_overrides_file(tmp_path).entries


def _plan_changes(tmp_path, run_cli, linear_id):
    """`generate`, then the field changes `deploy` would plan for `linear_id`."""
    from customer_registry_sync.deploy_plan import load_inputs, plan_for_deploy

    assert run_cli(["generate", "--out-dir", str(tmp_path), "--no-input"]) == 0
    inp = load_inputs(tmp_path, None, require_desired=True)
    live = [LINEAR_SCHEMA.state_to_live(r) for r in load_state(tmp_path)]
    plan = plan_for_deploy(inp, live, None).plan
    assert plan.errors == []
    return plan.changes[linear_id]


def _stub_then_notion(tmp_path, run_cli):
    """A real `ingest` makes Beta a stub; then a Notion record for it appears."""
    _tree(tmp_path, nodes=[BETA_NODE])
    assert _ingest(run_cli, tmp_path) == 0
    assert list(_stubs(tmp_path)) == ["a"]
    assert _identities(tmp_path)["a"].values["domains"] == ["a.test", "b.test"]
    _notion(tmp_path, BETA)


def test_handover_absorbs_stub_values(tmp_path, capsys, run_cli):
    _stub_then_notion(tmp_path, run_cli)
    capsys.readouterr()
    assert _ingest(run_cli, tmp_path) == 0
    assert _stubs(tmp_path) == {}
    assert "handover: 1 customers kept their Linear values" in capsys.readouterr().out
    entry = _overrides(tmp_path)["a"]
    assert entry.pin == {"revenue": 5000, "tier": "Gold"}
    # the identity already holds b.test, so the upstream row carries it: no add needed
    assert entry.add == {}
    changes = _plan_changes(tmp_path, run_cli, "lin_b")
    assert [c for c in changes if c.kind in ("clear", "remove")] == []


def test_handover_adds_a_stub_domain_the_identity_lacks(tmp_path, run_cli):
    _stub_then_notion(tmp_path, run_cli)
    # c.test reached Linear after the last ingest: the stub's identity never learned it
    _mirror(
        tmp_path,
        _node(
            "lin_b", "Beta Corp", domains=["a.test", "b.test", "c.test"], revenue=5000, tier="Gold"
        ),
    )
    assert _ingest(run_cli, tmp_path) == 0
    assert _overrides(tmp_path)["a"].add == {"domains": ["c.test"]}
    changes = _plan_changes(tmp_path, run_cli, "lin_b")
    assert [c for c in changes if c.kind in ("clear", "remove")] == []


@pytest.mark.parametrize(
    ("ops", "want_add", "want_remove"),
    [
        # a case variant already added is not written twice
        ("{add: [C.test]}", {"domains": ["C.test"]}, {}),
        # a curated remove stands: the handover adds nothing back
        ("{remove: [C.test]}", {}, {"domains": ["C.test"]}),
    ],
)
def test_handover_leaves_curated_domains(tmp_path, run_cli, ops, want_add, want_remove):
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    _stub_then_notion(tmp_path, run_cli)
    _mirror(
        tmp_path,
        _node(
            "lin_b", "Beta Corp", domains=["a.test", "b.test", "c.test"], revenue=5000, tier="Gold"
        ),
    )
    (tmp_path / OVERRIDES_FILE).write_text(f"customers:\n  a:\n    domains: {ops}\n")
    assert _ingest(run_cli, tmp_path) == 0
    entry = _overrides(tmp_path)["a"]
    assert entry.pin == {"revenue": 5000, "tier": "Gold"}
    assert entry.add == want_add
    assert entry.remove == want_remove


def test_handover_keeps_existing_pin(tmp_path, run_cli):
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    _stub_then_notion(tmp_path, run_cli)
    (tmp_path / OVERRIDES_FILE).write_text("customers:\n  a:\n    pin: {revenue: 7000}\n")
    assert _ingest(run_cli, tmp_path) == 0
    assert _overrides(tmp_path)["a"].pin == {"revenue": 7000, "tier": "Gold"}


def test_handover_is_once(tmp_path, run_cli):
    _stub_then_notion(tmp_path, run_cli)
    assert _ingest(run_cli, tmp_path) == 0
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    path = tmp_path / OVERRIDES_FILE
    path.write_text("customers:\n  a:\n    pin: {revenue: 5000}\n")
    # the stub is gone: a later run hands nothing over, so a hand edit stands
    assert _ingest(run_cli, tmp_path) == 0
    assert _overrides(tmp_path)["a"].pin == {"revenue": 5000}


def test_handover_dry_run_writes_nothing(tmp_path, capsys, run_cli):
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    _stub_then_notion(tmp_path, run_cli)
    capsys.readouterr()
    args = ["ingest", "--only", "linear-found", "--out-dir", str(tmp_path), "--dry-run"]
    assert run_cli(args) == 0
    assert "would keep the Linear values of 1 customers" in capsys.readouterr().out
    assert not (tmp_path / OVERRIDES_FILE).exists()


@pytest.mark.parametrize(
    "entry",
    [
        "hidden: {reason: test account}",
        "deleted: {linear_id: lin_b, confirmed: '2026-10-30'}",
    ],
)
def test_no_handover_when_stub_just_dropped(tmp_path, run_cli, entry):
    from customer_registry_sync.linear_overrides import OVERRIDES_FILE

    _stub_then_notion(tmp_path, run_cli)
    (tmp_path / OVERRIDES_FILE).write_text(f"customers:\n  a:\n    {entry}\n")
    assert _ingest(run_cli, tmp_path) == 0
    got = _overrides(tmp_path)["a"]
    assert got.pin == {}
    assert got.add == {}
