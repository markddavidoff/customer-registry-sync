import sys
from datetime import datetime

import yaml

from customer_registry_sync import manifest
from customer_registry_sync.datasets import NOTION, dataset_dir
from customer_registry_sync.manifest import (
    MANIFEST,
    guard_overwrite,
    manifest_edits,
    write_manifest,
)


def _dir_with_files(tmp_path):
    d = tmp_path / "linear"
    d.mkdir()
    (d / "a.yaml").write_text("name: A\n")
    (d / "b.yaml").write_text("name: B\n")
    return d


def _no_prompt(*_args):
    raise AssertionError("prompted")


def test_manifest_round_trip_clean(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    assert manifest_edits(d) == []
    body = yaml.safe_load((d / MANIFEST).read_text())
    assert body["generator"] == "generate"
    assert body["written_at"]
    assert sorted(body["files"]) == ["a.yaml", "b.yaml"]  # excludes the manifest itself
    assert all(len(h) == 64 for h in body["files"].values())


class _Clock:
    """A `datetime` stand-in for `manifest`: each `now()` is one hour later."""

    calls = 0

    @classmethod
    def now(cls, tz=None):
        cls.calls += 1
        return datetime(2026, 10, 1, cls.calls % 24, tzinfo=tz)


def _ticking_clock(monkeypatch):
    _Clock.calls = 0
    monkeypatch.setattr(manifest, "datetime", _Clock)


def test_write_manifest_is_byte_stable_when_files_unchanged(tmp_path, monkeypatch):
    _ticking_clock(monkeypatch)
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    first = (d / MANIFEST).read_bytes()
    mtime = (d / MANIFEST).stat().st_mtime_ns
    write_manifest(d, "generate")
    assert (d / MANIFEST).read_bytes() == first
    assert (d / MANIFEST).stat().st_mtime_ns == mtime  # nothing was written
    (d / "a.yaml").write_text("name: A2\n")
    write_manifest(d, "generate")
    body = yaml.safe_load((d / MANIFEST).read_text())
    assert body["written_at"] != yaml.safe_load(first)["written_at"]
    assert manifest_edits(d) == []


def test_each_generator_rerun_on_unchanged_inputs_keeps_manifests(tmp_path, monkeypatch, run_cli):
    """generate, ingest --only linear-found and sync-inbound twice: no manifest churn."""
    from customer_registry_sync import cli
    from customer_registry_sync.datasets import LINEAR_FOUND
    from customer_registry_sync.linear_client import parse_customer_node

    _ticking_clock(monkeypatch)
    (tmp_path / "datasets.yaml").write_text(
        "datasets: [notion, linear-found]\n"
        "linear_status_classification: {Active: real, Prospect: prospect, Churned: churned}\n"
    )
    _seed_dataset(tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    live = [
        parse_customer_node(
            {
                "id": "lin_b",
                "name": "Beta Corp",
                "domains": ["beta.example.test"],
                "revenue": None,
                "externalIds": [],
                "updatedAt": "2026-10-01T00:00:00.000Z",
                "status": {"id": "s", "name": "Active"},
                "tier": None,
            }
        )
    ]

    class _Client:
        def list_customers(self):
            return list(live)

    monkeypatch.setattr(cli, "_linear_client", lambda: _Client())
    runs = [
        ["sync-inbound"],
        ["ingest", "--only", "linear-found", "--no-input"],
        ["generate", "--no-input"],
    ]
    dirs = [tmp_path / "linear-state", dataset_dir(tmp_path, LINEAR_FOUND), tmp_path / "linear"]
    for argv in runs:
        assert run_cli([*argv, "--out-dir", str(tmp_path)]) == 0
    before = [(p / MANIFEST).read_bytes() for p in dirs]
    for argv in runs:
        assert run_cli([*argv, "--out-dir", str(tmp_path)]) == 0
    assert [(p / MANIFEST).read_bytes() for p in dirs] == before


def test_detects_modified_added_missing(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("name: A edited\n")
    (d / "b.yaml").unlink()
    (d / "c.yaml").write_text("name: C\n")
    assert manifest_edits(d) == ["added: c.yaml", "missing: b.yaml", "modified: a.yaml"]


def test_guard_refuses_without_tty(tmp_path, monkeypatch, capsys):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert guard_overwrite(d, "generate", no_input=False) is False
    assert "modified: a.yaml" in capsys.readouterr().out


def test_guard_refuses_with_no_input_even_on_tty(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert guard_overwrite(d, "generate", no_input=True) is False


def test_guard_prompts_and_accepts_y(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []

    def say_y(prompt=""):
        prompts.append(prompt)
        return " Y "

    monkeypatch.setattr("builtins.input", say_y)
    assert guard_overwrite(d, "generate", no_input=False) is True
    assert prompts == ["These files were edited by hand and will be replaced. Continue? [y/N] "]
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert guard_overwrite(d, "generate", no_input=False) is False
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert guard_overwrite(d, "generate", no_input=False) is False


def test_no_manifest_is_not_an_edit(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    assert manifest_edits(d) is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert guard_overwrite(d, "generate", no_input=True) is True
    assert guard_overwrite(tmp_path / "does-not-exist", "generate", no_input=True) is True


def test_clean_dir_passes_guard(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    assert guard_overwrite(d, "generate", no_input=True) is True


def test_writers_and_loaders_leave_the_manifest_alone(tmp_path):
    from customer_registry_sync.generate import generate_linear
    from customer_registry_sync.schema import CustomerRecord
    from customer_registry_sync.writers import (
        load_dataset,
        load_desired,
        write_dataset,
        write_desired,
    )

    recs = [
        CustomerRecord(
            canonical_name="Acmeco", domains=["acmeco.example.test"], classification="real"
        )
    ]
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_dataset(recs, tmp_path)  # a rewrite must neither delete nor choke on the manifest
    assert (dataset_dir(tmp_path, NOTION) / MANIFEST).exists()
    assert [r.canonical_name for r in load_dataset(tmp_path)] == ["Acmeco"]

    write_desired(generate_linear(recs), tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    write_desired(generate_linear(recs), tmp_path)
    assert (tmp_path / "linear" / MANIFEST).exists()
    assert [r.name for r in load_desired(tmp_path)] == ["Acmeco [Active]"]


def _seed_dataset(tmp_path):
    from customer_registry_sync.schema import CustomerRecord
    from customer_registry_sync.writers import write_dataset

    write_dataset(
        [
            CustomerRecord(
                canonical_name="Acmeco", domains=["acmeco.example.test"], classification="real"
            )
        ],
        tmp_path,
    )


def test_cli_generate_writes_manifest_and_refuses_over_hand_edits(tmp_path, capsys, run_cli):
    _seed_dataset(tmp_path)
    assert run_cli(["generate", "--out-dir", str(tmp_path), "--no-input"]) == 0
    assert manifest_edits(tmp_path / "linear") == []
    f = tmp_path / "linear" / "acmeco.yaml"
    f.write_text(f.read_text() + "# hand edit\n")
    assert run_cli(["generate", "--out-dir", str(tmp_path), "--no-input"]) == 2
    assert "modified: acmeco.yaml" in capsys.readouterr().out
    assert "# hand edit" in f.read_text()  # nothing was written
    # dry-run skips the guard
    assert run_cli(["generate", "--out-dir", str(tmp_path), "--dry-run", "--no-input"]) == 0


def test_refresh_state_warns_on_hand_edits_and_rewrites_manifest(tmp_path, capsys):
    from customer_registry_sync import cli
    from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced

    row = LinearCustomer(
        name="Acme",
        status="Active",
        external_ids=["acme"],
        linear_synced=LinearSynced("lin_1", "2026-10-01T00:00:00.000Z", "h"),
    )
    cli._refresh_state([row], tmp_path)
    assert manifest_edits(tmp_path / "linear-state") == []
    f = tmp_path / "linear-state" / "acme.yaml"
    f.write_text(f.read_text() + "# hand edit\n")
    cli._refresh_state([row], tmp_path)
    assert "WARNING" in capsys.readouterr().err
    assert manifest_edits(tmp_path / "linear-state") == []
    assert "# hand edit" not in f.read_text()
