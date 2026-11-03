"""`linear-overrides.yaml` bound for Linear customers: the format, pinning a name without
its status suffix, value types, and `generate` applying the overrides last.

The overrides mechanics are engine tests (`gitops_engine/tests/test_overrides.py`)."""

import ast
import inspect

import pytest
from gitops_engine.datasets import dataset_dir
from gitops_engine.manifest import write_manifest
from gitops_engine.overrides import load_overrides

from customer_registry_sync import linear_overrides
from customer_registry_sync.check import offline_errors, override_findings
from customer_registry_sync.datasets import NOTION
from customer_registry_sync.generate import generate_from_dir
from customer_registry_sync.linear_overrides import (
    OVERRIDES_FILE,
    OVERRIDES_FORMAT,
    base_value,
    load_overrides_file,
    pin_customer,
    value_type_failures,
)
from customer_registry_sync.linear_schema import LinearCustomer
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import write_csvs, write_dataset, write_desired, write_state


def _row(**kw):
    base = {
        "name": "Acme [Prospect]",
        "domains": ["acme.example.test"],
        "revenue": 10,
        "tier": None,
        "status": "Prospect",
        "external_ids": ["acme"],
    }
    base.update(kw)
    return LinearCustomer(**base)


def test_overrides_format():
    assert OVERRIDES_FILE == "linear-overrides.yaml"
    assert OVERRIDES_FORMAT.file == OVERRIDES_FILE
    assert (OVERRIDES_FORMAT.records_key, OVERRIDES_FORMAT.live_id_key) == (
        "customers",
        "linear_id",
    )
    assert OVERRIDES_FORMAT.pinnable == ("name", "status", "revenue", "tier")
    assert OVERRIDES_FORMAT.list_fields == ("domains",)
    assert OVERRIDES_FORMAT.header.startswith("#")


def test_pinned_name_keeps_status_suffix():
    row = _row(name="Acme [Prospect]", status="Prospect")
    assert pin_customer(row, {"name": "Acme Corp"}).name == "Acme Corp [Prospect]"
    pinned = pin_customer(row, {"name": "Acme Corp", "status": "Churned"})
    assert (pinned.name, pinned.status) == ("Acme Corp [Churned]", "Churned")


def test_pinned_status_alone_moves_the_suffix():
    pinned = pin_customer(_row(), {"status": "Active"})
    assert (pinned.name, pinned.status) == ("Acme [Active]", "Active")


def test_pin_customer_sets_optional_values_and_leaves_the_rest():
    row = _row(revenue=10, tier="Tier 1")
    pinned = pin_customer(row, {"revenue": 120000, "tier": None})
    assert (pinned.revenue, pinned.tier) == (120000, None)
    assert (pinned.name, pinned.domains, pinned.external_ids) == (
        row.name,
        row.domains,
        row.external_ids,
    )
    assert row.revenue == 10  # the input row is not changed


def test_base_value_strips_the_suffix_from_names_only():
    assert base_value("name", "Acme Corp [Active]") == "Acme Corp"
    assert base_value("name", "Acme Corp") == "Acme Corp"
    assert base_value("tier", "Tier 1 [Active]") == "Tier 1 [Active]"
    assert base_value("revenue", 5) == 5


def test_load_overrides_file_missing_is_empty(tmp_path):
    ov = load_overrides_file(tmp_path)
    assert ov.entries == {} and ov.file == OVERRIDES_FILE


def test_load_overrides_file_raises_on_a_bad_shape(tmp_path):
    (tmp_path / OVERRIDES_FILE).write_text("customers: [acme]\n")
    with pytest.raises(ValueError, match="linear-overrides.yaml"):
        load_overrides_file(tmp_path)


@pytest.mark.parametrize(
    ("pin", "message"),
    [
        ("revenue: lots", "revenue must be an integer or null"),
        ("revenue: 1.5", "revenue must be an integer or null"),
        ("revenue: true", "revenue must be an integer or null"),
        ("tier: 3", "tier must be a string or null"),
        ("name: ''", "name must be a non-empty string"),
        ("name: 7", "name must be a non-empty string"),
    ],
)
def test_value_type_failures(tmp_path, pin, message):
    (tmp_path / OVERRIDES_FILE).write_text(f"customers:\n  acme:\n    pin:\n      {pin}\n")
    [failure] = value_type_failures(load_overrides_file(tmp_path))
    assert failure == f"{OVERRIDES_FILE}:2: acme: pin {message}"


def test_value_types_that_pass(tmp_path):
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n  acme:\n    pin: {revenue: 120000, tier: null, name: Acme Corp}\n"
        "  globex:\n    pin: {revenue: null, tier: Tier 1}\n"
    )
    assert value_type_failures(load_overrides_file(tmp_path)) == []


def test_linear_overrides_does_not_import_generate():
    """`generate` imports this module; importing `generate` back would be circular."""
    tree = ast.parse(inspect.getsource(linear_overrides))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "generate" not in imported and "linear_files" not in imported


def _tree(tmp_path, recs):
    """A data dir as the real pipeline leaves it: dataset (ingest), then linear/ and the
    CSVs from `generate_from_dir`."""
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_state([], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    _regenerate(tmp_path)


def _regenerate(tmp_path):
    gen = generate_from_dir(tmp_path)
    write_desired(gen.rows, tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    return gen


def _acme(**kw):
    base = {
        "canonical_name": "Acme",
        "domains": ["acme.example.test", "old-acme.example.test"],
        "classification": "prospect",
        "arr": 10.0,
        "tier": "Tier 1",
    }
    base.update(kw)
    return CustomerRecord(**base)


def test_generate_applies_overrides_last(tmp_path):
    _tree(tmp_path, [_acme()])
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n"
        "  acme:\n"
        "    pin: {name: Acme Corp, revenue: 120000, tier: null}\n"
        "    domains:\n"
        "      add: [acme.io, ACME.example.test]\n"  # re-cased: already there
        "      remove: [OLD-ACME.example.test]\n"  # re-cased: still matches
    )
    assert offline_errors(tmp_path) == [
        "linear out of date: acme: run generate"  # the overrides file is a generate input
    ]
    gen = _regenerate(tmp_path)
    [row] = gen.rows
    assert row.name == "Acme Corp [Prospect]"
    assert (row.revenue, row.tier) == (120000, None)
    assert row.domains == ["acme.example.test", "acme.io"]
    # upstream is the rows before overrides; the CSVs stay built from the winners
    [up] = gen.upstream
    assert (up.name, up.revenue, up.tier) == ("Acme [Prospect]", 10, "Tier 1")
    assert [w.record.arr for w in gen.winners] == [10.0]
    assert offline_errors(tmp_path) == []
    findings = override_findings(tmp_path)  # the re-cased remove is carried upstream
    assert (findings.failures, findings.reports, findings.pending) == ([], [], [])


def test_generate_drops_hidden_and_deleted_rows(tmp_path):
    _tree(tmp_path, [_acme(), _acme(canonical_name="Globex", domains=["globex.example.test"])])
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n"
        "  acme:\n    hidden: {reason: test account}\n"
        "  globex:\n    deleted: {linear_id: lin_g, confirmed: 2026-10-28}\n"
    )
    gen = generate_from_dir(tmp_path)
    assert gen.rows == []
    assert sorted(r.external_ids[0] for r in gen.upstream) == ["acme", "globex"]


def test_generate_cli_refuses_an_invalid_overrides_file(tmp_path, capsys, run_cli):
    _tree(tmp_path, [_acme()])
    (tmp_path / OVERRIDES_FILE).write_text("customers:\n  acme:\n    pin: {status: null}\n")
    before = (tmp_path / "linear" / "acme.yaml").read_text()
    assert run_cli(["generate", "--out-dir", str(tmp_path), "--no-input"]) == 2
    err = capsys.readouterr().err
    assert "status cannot be pinned empty" in err
    assert (tmp_path / "linear" / "acme.yaml").read_text() == before


def test_overrides_file_round_trips_through_the_engine_loader(tmp_path):
    (tmp_path / OVERRIDES_FILE).write_text(
        "customers:\n  acme:\n    pending: {revenue: 150000, domains_add: [acme.dev]}\n"
    )
    ov = load_overrides(tmp_path / OVERRIDES_FILE, OVERRIDES_FORMAT)
    assert ov.entries["acme"].pending == {"revenue": 150000, "domains_add": ["acme.dev"]}
