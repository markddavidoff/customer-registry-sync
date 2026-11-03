"""`hide --linear-id <id> --reason <text>`: take a mirrored Linear customer out of the sync.

The data dirs are built by the real producers (identity store, dataset writer, the mirror
writer, `ingest --only linear-found`, `generate`); the overrides file is the YAML a person
would write."""

import pytest
from gitops_engine.datasets import dataset_dir
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync import cli
from customer_registry_sync.check import orphan_lines
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetConfig, load_datasets
from customer_registry_sync.generate import generate_from_dir
from customer_registry_sync.identity import IDENTITIES_FILE, IdentityStore, load_identities
from customer_registry_sync.linear_found import status_errors
from customer_registry_sync.linear_overrides import OVERRIDES_FILE, load_overrides_file
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    STATE_SUBDIR,
    write_csvs,
    write_dataset,
    write_desired,
    write_state,
)

DATASETS_YAML = "datasets: [notion, linear-found]\nlinear_status_classification: {Active: real}\n"
ACME = CustomerRecord(
    canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
)


def _files(data):
    return {
        str(p.relative_to(data)): p.read_bytes() for p in sorted(data.rglob("*")) if p.is_file()
    }


def _tree(data, recording_linear, *extra, identities=()):
    """A data dir holding Acme (paired to a live row) plus the mirrored `extra` customers
    ((id, name) pairs) that no dataset lists. Returns the live nodes."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "datasets.yaml").write_text(DATASETS_YAML)
    store = IdentityStore(data / IDENTITIES_FILE)
    store.add_identity("acme", names=["Acme"], domains=["acme.example.test"])
    for cid, kwargs in identities:
        store.add_identity(cid, **kwargs)
    store.save()
    write_dataset([ACME], data, dataset=NOTION)
    write_manifest(dataset_dir(data, NOTION), "ingest")
    write_dataset([], data, dataset=LINEAR_FOUND)
    write_manifest(dataset_dir(data, LINEAR_FOUND), "ingest")
    gen = generate_from_dir(data)
    write_desired(gen.rows, data)
    write_csvs(gen.winners, data)
    write_manifest(data / "linear", "generate")
    [d] = gen.rows
    live = [
        recording_linear.node(
            "lin_a", d.name, domains=list(d.domains), revenue=1, xids=d.external_ids
        )
    ]
    live += [recording_linear.node(lid, name) for lid, name in extra]
    write_state(reconcile_mirror(live, LINEAR_SCHEMA), data)
    write_manifest(data / STATE_SUBDIR, "sync-inbound")
    return live


def _hide(run_cli, data, lid="lin_x", reason="a test account"):
    return run_cli(["hide", "--linear-id", lid, "--reason", reason, "--out-dir", str(data)])


def _desired_names(data):
    return sorted(p.name for p in (data / "linear").glob("*.yaml") if not p.name.startswith("."))


def _hidden(data):
    return {cid: e.hidden for cid, e in load_overrides_file(data).entries.items() if e.hidden}


def test_hide_existing_identity(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    ident = ("globex", {"names": ["Globex"], "sources": {"linear": "lin_x"}})
    _tree(data, recording_linear, ("lin_x", "Globex"), identities=[ident])
    identities = (data / IDENTITIES_FILE).read_bytes()
    assert _hide(run_cli, data) == 0
    assert capsys.readouterr().out == (
        "hidden 'Globex' (lin_x) as globex; run generate, then check\n"
    )
    assert _hidden(data) == {"globex": "a test account"}
    assert (data / IDENTITIES_FILE).read_bytes() == identities  # no new identity


def test_hide_resolves_through_identity_keys_not_only_the_linear_id(
    tmp_path, run_cli, recording_linear
):
    # the identity names the customer but records no Linear id yet
    data = tmp_path / "data"
    _tree(
        data, recording_linear, ("lin_x", "Globex"), identities=[("globex", {"names": ["Globex"]})]
    )
    assert _hide(run_cli, data) == 0
    assert _hidden(data) == {"globex": "a test account"}
    assert set(load_identities(data / IDENTITIES_FILE).entries) == {"acme", "globex"}


def test_hide_appends_identity(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    _tree(data, recording_linear, ("lin_x", "Acme Sandbox [Active]"), ("lin_y", "Acme Sandbox"))
    assert _hide(run_cli, data) == 0
    assert capsys.readouterr().out == (
        "hidden 'Acme Sandbox [Active]' (lin_x) as acme-sandbox; run generate, then check\n"
    )
    assert _hide(run_cli, data, "lin_y", "demo") == 0
    table = load_identities(data / IDENTITIES_FILE).entries
    assert table["acme-sandbox"].sources == {"linear": "lin_x"}
    assert table["acme-sandbox-2"].sources == {"linear": "lin_y"}
    assert _hidden(data) == {"acme-sandbox": "a test account", "acme-sandbox-2": "demo"}


def test_hide_already_hidden_exits_4(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    _tree(data, recording_linear, ("lin_x", "Globex"))
    assert _hide(run_cli, data, reason="first reason") == 0
    capsys.readouterr()
    before = _files(data)
    assert _hide(run_cli, data, reason="second reason") == 4
    captured = capsys.readouterr()
    assert "already hidden" in captured.out + captured.err
    assert _hidden(data) == {"globex": "first reason"}
    assert _files(data) == before


def test_hide_unmirrored_id_exits_2(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    _tree(data, recording_linear)
    before = _files(data)
    assert _hide(run_cli, data, "lin_nope") == 2
    assert "lin_nope" in capsys.readouterr().err
    assert _files(data) == before


@pytest.mark.parametrize("reason", ["", "   "])
def test_hide_empty_reason_exits_2(tmp_path, run_cli, recording_linear, capsys, reason):
    data = tmp_path / "data"
    _tree(data, recording_linear, ("lin_x", "Globex"))
    before = _files(data)
    assert _hide(run_cli, data, reason=reason) == 2
    assert "reason" in capsys.readouterr().err
    assert _files(data) == before


def test_hide_invalid_overrides_exits_2(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    _tree(data, recording_linear, ("lin_x", "Globex"))
    (data / OVERRIDES_FILE).write_text("customers:\n  acme:\n    pin: {domains: [a.test]}\n")
    before = _files(data)
    assert _hide(run_cli, data) == 2
    assert OVERRIDES_FILE in capsys.readouterr().err
    assert _files(data) == before


def test_hide_refuses_a_legacy_ignore_file(tmp_path, run_cli, recording_linear, capsys):
    data = tmp_path / "data"
    _tree(data, recording_linear, ("lin_x", "Globex"))
    (data / "linear-ignore.yaml").write_text("customers: []\n")
    before = _files(data)
    assert _hide(run_cli, data) == 2
    assert "run adopt-live" in capsys.readouterr().err
    assert _files(data) == before


def test_orphan_and_status_messages_name_hide(tmp_path, recording_linear):
    orphan = "\n".join(orphan_lines(recording_linear.node("lin_hooli", "Hooli")))
    assert 'hide --linear-id lin_hooli --reason "<why>"' in orphan
    assert "ignore" not in orphan
    odd = CustomerRecord(
        canonical_name="Odd",
        status="Paused",
        domains=[],
        linear_customer_id="lin_s",
        sources=["linear:lin_s"],
    )
    write_dataset([odd], tmp_path, dataset=LINEAR_FOUND)
    cfg = DatasetConfig(names=[NOTION, LINEAR_FOUND], status_classification={"Active": "real"})
    [msg] = status_errors(load_datasets(tmp_path, cfg), cfg)
    assert 'hide --linear-id lin_s --reason "<why>"' in msg
    assert "ignore" not in msg


def test_hide_then_check_passes(tmp_path, run_cli, recording_linear, monkeypatch, capsys):
    data = tmp_path / "data"
    live = _tree(data, recording_linear, ("lin_x", "Globex"))
    monkeypatch.setattr(cli, "_linear_client", lambda: recording_linear(live))
    stub_argv = ["ingest", "--only", LINEAR_FOUND, "--out-dir", str(data), "--no-input"]
    gen_argv = ["generate", "--out-dir", str(data), "--no-input"]

    # before: ingest stubs the customer, so generate gives it a desired row
    assert run_cli(stub_argv) == 0
    assert run_cli(gen_argv) == 0
    assert _desired_names(data) == ["acme.yaml", "globex.yaml"]

    assert _hide(run_cli, data) == 0
    capsys.readouterr()
    assert run_cli(stub_argv) == 0
    assert run_cli(gen_argv) == 0
    assert _desired_names(data) == ["acme.yaml"]

    assert run_cli(["check", "--out-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "  HIDDEN: 'Globex' (lin_x): a test account\n" in out
    assert "ERROR: orphan" not in out and "DRIFT" not in out
    assert out.rstrip().endswith("IN SYNC")
