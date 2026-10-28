from datetime import datetime

from gitops_engine import manifest
from gitops_engine.datasets import dataset_dir
from gitops_engine.manifest import (
    MANIFEST,
    manifest_edits,
    write_manifest,
)

from customer_registry_sync.datasets import NOTION


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
