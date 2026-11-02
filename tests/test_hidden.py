"""Hidden customers: `hidden` entries and `hidden_name_prefixes` in `linear-overrides.yaml`
take a customer out of the sync on both sides, in place of `linear-ignore.yaml`.

A live customer is hidden by id only through the identity table: its keys must resolve to
the canonical id the `hidden` entry names. The data dirs here are built by the real
producers (identity store, dataset writer, `generate`, the mirror writer); the overrides
file is curated input, so it is written as the YAML a person would write."""

import pytest
from gitops_engine.datasets import dataset_dir
from gitops_engine.ignore import BUILTIN_PREFIX
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync import cli
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetConfig, load_datasets
from customer_registry_sync.deploy_plan import (
    check_for,
    load_inputs,
    offline_plan,
    partitioned,
    plan_for_deploy,
)
from customer_registry_sync.generate import generate_from_dir, merge_datasets
from customer_registry_sync.identity import IDENTITIES_FILE, IdentityStore, load_identities
from customer_registry_sync.linear_files import (
    LEGACY_FILES,
    hidden_conflicts,
    hidden_lines,
    partition_hidden,
)
from customer_registry_sync.linear_overrides import OVERRIDES_FILE
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    STATE_SUBDIR,
    write_csvs,
    write_dataset,
    write_desired,
    write_state,
)

DATASETS_YAML = """\
datasets: [notion, linear-found]
linear_status_classification:
  Active: real
  Prospect: prospect
  Churned: churned
"""
BOTH = DatasetConfig(names=[NOTION, LINEAR_FOUND])

# globex is hidden by canonical id (line 3); `sandbox-` is a file prefix (line 6).
OVERRIDES = """\
customers:
  globex:
    hidden: {reason: test account}
hidden_name_prefixes:
  - prefix: "sandbox-"
    reason: manual experiments
"""

ACME = CustomerRecord(
    canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
)


def _identities(tmp_path, *identities):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        store.add_identity(cid, names=names, domains=domains, sources=sources)
    store.save()


def _tree(tmp_path, records, *identities, overrides=OVERRIDES):
    """identities.yaml, datasets.yaml, the notion dataset, the overrides file, then
    `generate`'s outputs; returns the desired rows."""
    (tmp_path / "datasets.yaml").write_text(DATASETS_YAML)
    _identities(tmp_path, *identities)
    write_dataset(list(records), tmp_path, dataset=NOTION)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_dataset([], tmp_path, dataset=LINEAR_FOUND)
    write_manifest(dataset_dir(tmp_path, LINEAR_FOUND), "ingest")
    (tmp_path / OVERRIDES_FILE).write_text(overrides)
    return _generate(tmp_path)


def _generate(tmp_path):
    gen = generate_from_dir(tmp_path)
    write_desired(gen.rows, tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    return gen.rows


def _mirror(tmp_path, *nodes):
    write_state(reconcile_mirror(list(nodes), LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / STATE_SUBDIR, "sync-inbound")


def _acme_live(recording_linear, d):
    return recording_linear.node(
        "lin_1", d.name, domains=list(d.domains), revenue=1, xids=d.external_ids
    )


def _bytes(tmp_path):
    return {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }


GLOBEX_ID = ("globex", ["Globex"], [], {"linear": "lin_g"})


def test_hidden_customer_not_planned_not_orphan_not_stubbed(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    [d] = _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}), GLOBEX_ID)
    acme = _acme_live(recording_linear, d)
    globex = recording_linear.node("lin_g", "Globex", domains=["globex.example.test"], revenue=9)
    _mirror(tmp_path, acme, globex)

    # the refresh gives the hidden customer no stub
    assert (
        run_cli(["ingest", "--only", LINEAR_FOUND, "--out-dir", str(tmp_path), "--no-input"]) == 0
    )
    assert [e.source_id for e in load_datasets(tmp_path, BOTH) if e.dataset == LINEAR_FOUND] == []
    assert [r.name for r in _generate(tmp_path)] == [d.name]

    # not planned, not an orphan, not drift: listed as hidden
    plan, code = offline_plan(tmp_path)
    assert code == 0
    assert "lin_g" not in {lv.id for _, lv in plan.updates} | {o.id for o in plan.orphans}
    assert [c.name for c in plan.creates] == []
    inp = load_inputs(tmp_path, None)
    res = check_for(inp, [acme, globex], None)
    assert res.orphans == [] and [lv.id for lv in res.ignored] == ["lin_g"]

    monkeypatch.setattr(cli, "_linear_client", lambda: recording_linear([acme, globex]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "  HIDDEN: 'Globex' (lin_g): test account\n" in out
    assert "  HIDDEN PREFIX: sandbox- (linear-overrides.yaml:5): manual experiments\n" in out
    assert "ERROR: orphan" not in out and "DRIFT added" not in out

    # control: without the hidden entry the same customer is stubbed
    (tmp_path / OVERRIDES_FILE).write_text("customers: {}\n")
    assert (
        run_cli(["ingest", "--only", LINEAR_FOUND, "--out-dir", str(tmp_path), "--no-input"]) == 0
    )
    assert [e.source_id for e in load_datasets(tmp_path, BOTH) if e.dataset == LINEAR_FOUND] == [
        "globex"
    ]


def test_hidden_by_id_needs_the_identity_table(tmp_path, recording_linear):
    # the entry names a canonical id; a live row reaches it only by resolving through
    # identities.yaml (here: by its recorded Linear id, after a rename)
    [d] = _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}), GLOBEX_ID)
    inp = load_inputs(tmp_path, None)
    renamed = recording_linear.node("lin_g", "Globex Renamed")
    stranger = recording_linear.node("lin_x", "Globex Two")  # resolves to no identity
    managed, hidden = partition_hidden([renamed, stranger], inp.hidden, inp.table, scope=None)
    assert [lv.id for lv in hidden] == ["lin_g"] and [lv.id for lv in managed] == ["lin_x"]


def test_hidden_prefix_from_file(tmp_path, monkeypatch, capsys, recording_linear, run_cli):
    [d] = _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}), GLOBEX_ID)
    acme = _acme_live(recording_linear, d)
    lab = recording_linear.node("lin_s", "sandbox-lab", domains=["lab.example.test"])
    _mirror(tmp_path, acme, lab)
    inp = load_inputs(tmp_path, None)
    managed, hidden, other = partitioned([acme, lab], inp.hidden, inp.table, None)
    assert [lv.id for lv in managed] == ["lin_1"] and [lv.id for lv in hidden] == ["lin_s"]
    assert [lv.id for lv in other] == ["lin_s"]  # its domains still count as held
    # a file prefix applies under every scope, a matching one included
    assert partition_hidden([lab], inp.hidden, inp.table, scope="sandbox-")[1] == [lab]
    assert hidden_lines(hidden, inp.hidden, inp.table, scope=None) == [
        "HIDDEN: 'sandbox-lab' (lin_s): manual experiments",
        f"HIDDEN PREFIX: {BUILTIN_PREFIX} (built-in)",
        "HIDDEN PREFIX: sandbox- (linear-overrides.yaml:5): manual experiments",
    ]
    monkeypatch.setattr(cli, "_linear_client", lambda: recording_linear([acme, lab]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "  HIDDEN: 'sandbox-lab' (lin_s): manual experiments\n" in out
    assert "ERROR: orphan" not in out


def test_builtin_prefix_scope_exemption(tmp_path, recording_linear):
    _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}), GLOBEX_ID)
    inp = load_inputs(tmp_path, None)
    scope = f"{BUILTIN_PREFIX}1a2b3c4d-"
    e2e = recording_linear.node("lin_e2e", f"{scope}Acme")
    globex = recording_linear.node("lin_g", "Globex")
    lab = recording_linear.node("lin_s", "sandbox-lab")
    live = [e2e, globex, lab]
    # unscoped, the built-in prefix hides the e2e row
    managed, hidden = partition_hidden(live, inp.hidden, inp.table, scope=None)
    assert managed == [] and [lv.id for lv in hidden] == ["lin_e2e", "lin_g", "lin_s"]
    # its own scoped run manages it; ids and file prefixes still hide
    managed, hidden = partition_hidden(live, inp.hidden, inp.table, scope=scope)
    assert [lv.id for lv in managed] == ["lin_e2e"]
    assert [lv.id for lv in hidden] == ["lin_g", "lin_s"]
    assert f"HIDDEN PREFIX: {BUILTIN_PREFIX} (built-in)" not in hidden_lines(
        hidden, inp.hidden, inp.table, scope=scope
    )
    # a scope that does not start with the built-in prefix keeps it in force
    assert partition_hidden([e2e], inp.hidden, inp.table, scope="Acme")[1] == [e2e]


def test_hidden_prefix_conflict_refuses_deploy(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    consulting = CustomerRecord(
        canonical_name="Sandbox Consulting",
        domains=["lab.example.test"],
        classification="real",
        arr=5.0,
    )
    _tree(tmp_path, [consulting])
    lab = recording_linear.node("lin_s", "sandbox-lab", domains=["lab.example.test"], revenue=1)
    _mirror(tmp_path, lab)
    inp = load_inputs(tmp_path, None)
    planning = plan_for_deploy(inp, [lab], None)
    msg = (
        "desired record 'Sandbox Consulting' shares domain 'lab.example.test' with hidden "
        "Linear customer 'sandbox-lab' (prefix 'sandbox-', linear-overrides.yaml:5). "
        "Remove that prefix from linear-overrides.yaml or change Sandbox Consulting."
    )
    assert planning.conflicts == [msg]
    fake = recording_linear([lab])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 3
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert f"  ERROR: {msg}\n" in out
    assert "REFUSED — hidden conflict(s) above; nothing written. Fix linear-overrides.yaml" in out
    assert [c for c in fake.calls if c[0] != "get"] == []
    assert not (tmp_path / "linear-snapshots").exists()


def test_hidden_id_conflict_and_own_stub(tmp_path, recording_linear):
    # a stubbed customer that is then hidden: its own stub is reported, not failed
    _identities(
        tmp_path,
        ("umbrella", ["Umbrella Analytics"], ["umbrella.example.test"], {"linear": "lin_umb"}),
    )
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n  umbrella:\n    hidden: {reason: a test org}\n"
    )
    stub = CustomerRecord(
        canonical_name="Umbrella Analytics",
        domains=["umbrella.example.test"],
        classification="real",
        linear_customer_id="lin_umb",
        sources=["linear:lin_umb"],
    )
    from customer_registry_sync.datasets import DatasetRecord

    entries = [DatasetRecord(dataset=LINEAR_FOUND, source_id="umbrella", record=stub)]
    table = load_identities(tmp_path / IDENTITIES_FILE)
    desired = merge_datasets(entries, table, [NOTION, LINEAR_FOUND]).rows
    umb = recording_linear.node(
        "lin_umb", "Umbrella Analytics", domains=["umbrella.example.test"], xids=["umbrella"]
    )
    inp = load_inputs(tmp_path, None)
    _, hidden = partition_hidden([umb], inp.hidden, table, scope=None)
    assert hidden == [umb]
    failures, reports = hidden_conflicts(hidden, inp.hidden, entries, desired, table, scope=None)
    assert failures == []
    assert reports == [
        "stub 'Umbrella Analytics' is for hidden Linear customer 'Umbrella Analytics': "
        "the next refresh drops it"
    ]


@pytest.mark.parametrize("legacy", LEGACY_FILES)
def test_ignore_file_present_refuses(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli, legacy
):
    [d] = _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}))
    acme = _acme_live(recording_linear, d)
    _mirror(tmp_path, acme)
    (tmp_path / legacy).write_text("customers: []\n")
    fake = recording_linear([acme])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    before = _bytes(tmp_path)
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 2
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err.count(f"ERROR: {legacy} is no longer read by v0.3.0: run adopt-live") == 2
    with pytest.raises(ValueError, match="cannot plan"):
        offline_plan(tmp_path)
    assert fake.calls == [] and _bytes(tmp_path) == before


@pytest.mark.parametrize("legacy", LEGACY_FILES)
@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "--only", LINEAR_FOUND, "--no-input"],
        ["sync-inbound", "--no-input"],
        ["refresh", "--no-input"],
    ],
    ids=["ingest", "sync-inbound", "refresh"],
)
def test_legacy_file_refuses_ingest_sync_inbound_refresh(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli, legacy, argv
):
    # Without the guard each command would write: the mirror holds an unstubbed
    # customer, and live Linear holds one the mirror lacks.
    [d] = _tree(tmp_path, [ACME], ("acme", ["Acme"], ["acme.example.test"], {}))
    acme = _acme_live(recording_linear, d)
    _mirror(tmp_path, acme, recording_linear.node("lin_b", "Beta Corp"))
    (tmp_path / legacy).write_text("customers: []\n")
    fake = recording_linear([acme, recording_linear.node("lin_n", "New Co")])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    before = _bytes(tmp_path)
    assert run_cli(argv, data_dir=tmp_path) == 2
    assert f"ERROR: {legacy} is no longer read by v0.3.0: run adopt-live" in capsys.readouterr().err
    assert _bytes(tmp_path) == before
    assert fake.calls == []


def test_ignore_subcommand_is_gone(tmp_path, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["ignore", "--linear-id", "lin_x", "--reason", "r", "--out-dir", str(tmp_path)])
    assert exc.value.code == 2
    assert "ignore" not in cli.build_parser()[1]
