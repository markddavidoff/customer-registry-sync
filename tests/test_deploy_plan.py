"""`offline_plan`: deploy's plan and check's exit with the state mirror as live Linear.

The data dirs are built by the real producers (dataset -> generate -> mirror), and
each result is held against what `deploy` (dry run) and `check` do through the CLI
with a fake client listing the same customers.
"""

from dataclasses import replace

import pytest
from gitops_engine.datasets import dataset_dir
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync import cli
from customer_registry_sync.deploy_plan import load_state, offline_plan, state_to_live
from customer_registry_sync.datasets import NOTION
from customer_registry_sync.generate import generate_from_dir
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_files import IGNORE_FILE
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import write_csvs, write_dataset, write_desired, write_state


class _Fake:
    def __init__(self, live):
        self.live = live

    def list_customers(self):
        return list(self.live)


def _tree(tmp_path) -> list[LiveCustomer]:
    recs = [
        CustomerRecord(
            canonical_name=name, domains=[f"{slug}.example.test"], classification="real", arr=1.0
        )
        for name, slug in (("Acme", "acme"), ("Globex", "globex"), ("Initech", "initech"))
    ]
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    gen = generate_from_dir(tmp_path)
    write_desired(gen.rows, tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    live = [
        LiveCustomer(
            id=f"lin_{i}",
            name=d.name,
            domains=list(d.domains),
            revenue=d.revenue,
            tier=d.tier,
            status=d.status,
            external_ids=list(d.external_ids),
            updated_at="2026-10-01T00:00:00.000Z",
        )
        for i, d in enumerate(gen.rows)
    ]
    write_state(reconcile_mirror(live, LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    return live


def _cli(run_cli, monkeypatch, tmp_path, live, *cmd):
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake(live))
    return run_cli([*cmd, "--out-dir", str(tmp_path)])


def test_in_sync_plans_nothing_and_exits_0(tmp_path, monkeypatch, run_cli):
    live = _tree(tmp_path)
    plan, code = offline_plan(tmp_path)
    assert [d.name for d in plan.unchanged] == [
        "Acme [Active]",
        "Globex [Active]",
        "Initech [Active]",
    ]
    assert not (plan.creates or plan.updates or plan.aborts or plan.orphans or plan.errors)
    assert code == 0
    assert _cli(run_cli, monkeypatch, tmp_path, live, "check") == code
    assert [state_to_live(r) for r in load_state(tmp_path)] == live


def test_live_stand_in_gives_deploys_plan_and_checks_exit(tmp_path, monkeypatch, capsys, run_cli):
    live = _tree(tmp_path)
    acme, globex, _ = live
    moved = [
        replace(acme, revenue=7, updated_at="2026-10-09T00:00:00.000Z"),  # drift: abort
        replace(globex, name="Globex Corp"),  # a name the registry owns: update
        LiveCustomer("lin_new", "Newco", ["newco.example.org"], None, None, "Active", [], "v"),
    ]  # Initech is gone from Linear: a create
    plan, code = offline_plan(tmp_path, live=moved)
    assert [d.name for d in plan.creates] == ["Initech [Active]"]
    assert [(t.name, lv.name) for t, lv in plan.updates] == [("Globex [Active]", "Globex Corp")]
    assert [d.name for d, _, _ in plan.aborts] == ["Acme [Active]"]
    assert [lv.name for lv in plan.orphans] == ["Newco"]
    assert code == 3
    assert _cli(run_cli, monkeypatch, tmp_path, moved, "check") == code
    capsys.readouterr()
    assert _cli(run_cli, monkeypatch, tmp_path, moved, "deploy") == 3  # aborts
    out = capsys.readouterr().out
    assert cli.render_plan(plan) == [
        "+ Initech [Active]",
        "~ Globex [Active]",
        "    name: 'Globex Corp' -> 'Globex [Active]'",
    ]
    assert "\n".join(cli.render_plan(plan)) in out


def test_offline_errors_fail_the_check_exit(tmp_path):
    _tree(tmp_path)
    p = tmp_path / "linear" / "acme.yaml"
    p.write_text(p.read_text() + "# tampered\n")
    plan, code = offline_plan(tmp_path)
    assert len(plan.unchanged) == 3 and code == 3


def test_scope_plans_only_in_scope_rows(tmp_path):
    _tree(tmp_path)
    plan, code = offline_plan(tmp_path, scope="Glo")
    assert [d.name for d in plan.unchanged] == ["Globex [Active]"] and code == 0


def test_unreadable_ignore_file_raises(tmp_path, capsys):
    _tree(tmp_path)
    (tmp_path / IGNORE_FILE).write_text("customers: [")
    with pytest.raises(ValueError, match="cannot plan"):
        offline_plan(tmp_path)
    assert "ERROR:" in capsys.readouterr().err
