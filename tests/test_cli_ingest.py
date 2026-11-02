"""`ingest` through the configured source: a stub handing over to Notion."""

import yaml
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync import cli
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetConfig, load_datasets
from customer_registry_sync.deploy_plan import load_inputs, plan_for_deploy
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_overrides import load_overrides_file
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.writers import STATE_SUBDIR, load_state, write_state


def _globex_node():
    """Globex in Linear: carries values the fake Notion page for it lacks."""
    return parse_customer_node(
        {
            "id": "lin_g",
            "name": "Globex Labs",
            "domains": ["globexlabs.example.test", "globex-two.example.test"],
            "revenue": 5000,
            "externalIds": [],
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s", "name": "Prospect"},
            "tier": {"id": "t", "name": "Gold"},
        }
    )


def _setup(tmp_path, buckets):
    data = tmp_path / "data"
    data.mkdir()
    (data / "datasets.yaml").write_text("datasets: [notion, linear-found]\n")
    write_state(reconcile_mirror([_globex_node()], LINEAR_SCHEMA), data)
    write_manifest(data / STATE_SUBDIR, "sync-inbound")
    cfg = tmp_path / "registry-sync.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "source": {"kind": "notion", "hub_page_id": "hub", "buckets": buckets},
                "data_dir": str(data),
            }
        )
    )
    return data, ["--config", str(cfg)]


def test_ingest_only_notion_hands_over(fake_notion, tmp_path, capsys):
    data, cfg = _setup(tmp_path, fake_notion)
    # no Notion dataset yet: Globex is a stub
    assert cli.main([*cfg, "ingest", "--only", LINEAR_FOUND, "--no-input"]) == 0
    both = DatasetConfig(names=[NOTION, LINEAR_FOUND])
    stubs = [e.source_id for e in load_datasets(data, both) if e.dataset == LINEAR_FOUND]
    assert stubs == ["globexlabs"]
    capsys.readouterr()

    # the Notion page for Globex (no revenue, no tier) now resolves to the stub
    assert cli.main([*cfg, "ingest", "--only", NOTION, "--no-input"]) == 0
    assert "handover: 1 customers kept their Linear values" in capsys.readouterr().out
    entry = load_overrides_file(data).entries["globexlabs"]
    assert entry.pin == {"revenue": 5000, "tier": "Gold"}

    assert cli.main([*cfg, "generate", "--no-input"]) == 0
    inp = load_inputs(data, None, require_desired=True)
    live = [LINEAR_SCHEMA.state_to_live(r) for r in load_state(data)]
    plan = plan_for_deploy(inp, live, None).plan
    assert plan.errors == []
    assert [c for c in plan.changes["lin_g"] if c.kind in ("clear", "remove")] == []
