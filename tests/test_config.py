from pathlib import Path

import pytest
import yaml

from customer_registry_sync import cli
from customer_registry_sync.config import (
    ConfigError,
    RegistryConfig,
    WorkflowConfig,
    config_value,
    load_config,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "registry-sync.example.yaml"

BUCKET_NAMES = [
    "Customers",
    "Churned Customers",
    "Archive",
    "POCs",
    "Prospects",
    "Partner & Supporting Accounts",
]


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _valid() -> dict:
    return {
        "hub_page_id": "11111111-1111-4111-8111-111111111111",
        "buckets": {
            name: f"22222222-2222-4222-8222-{i:012d}" for i, name in enumerate(BUCKET_NAMES)
        },
        "data_dir": "registry-data",
    }


def test_load_config_reads_ids_from_yaml(tmp_path):
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", _valid()))
    assert isinstance(cfg, RegistryConfig)
    assert cfg.hub_page_id == "11111111-1111-4111-8111-111111111111"
    assert list(cfg.buckets) == BUCKET_NAMES
    assert cfg.buckets["Prospects"] == "22222222-2222-4222-8222-000000000004"
    # relative data_dir resolves against the directory holding the config file
    assert cfg.data_dir == tmp_path / "registry-data"


def test_data_dir_defaults_to_customers(tmp_path):
    data = _valid()
    del data["data_dir"]
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.data_dir == tmp_path / "customers"


def test_paths_resolve_under_data_dir(tmp_path, monkeypatch):
    """A relative data_dir anchors to the config file, not to the cwd the command runs from."""
    from gitops_engine.datasets import dataset_dir

    from customer_registry_sync.datasets import NOTION
    from customer_registry_sync.identity import IDENTITIES_FILE
    from customer_registry_sync.identity import IdentityStore
    from customer_registry_sync.schema import CustomerRecord
    from customer_registry_sync.writers import write_dataset

    cfg_dir = tmp_path / "repo"
    cfg_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = load_config(_write(cfg_dir / "registry-sync.yaml", _valid()))
    monkeypatch.chdir(elsewhere)

    data_dir = cfg_dir / "registry-data"
    assert cfg.data_dir == data_dir
    assert dataset_dir(cfg.data_dir, NOTION) == data_dir / "datasets" / "notion"
    assert (cfg.data_dir / IDENTITIES_FILE).parent == data_dir

    write_dataset([CustomerRecord(canonical_name="Acme", classification="real")], cfg.data_dir)
    store = IdentityStore(cfg.data_dir / IDENTITIES_FILE)
    store.add_identity("acme", names=["Acme"], domains=[], sources={})
    assert store.save() is True
    assert (data_dir / "datasets" / "notion" / "acme.yaml").is_file()
    assert (data_dir / IDENTITIES_FILE).is_file()
    assert list(elsewhere.iterdir()) == []  # nothing leaked into the cwd


def test_absolute_data_dir_is_kept(tmp_path):
    data = _valid()
    data["data_dir"] = str(tmp_path / "elsewhere")
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.data_dir == tmp_path / "elsewhere"


def test_missing_config_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="nope.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_missing_config_file_exits_2(tmp_path, capsys):
    missing = tmp_path / "nope.yaml"
    assert cli.main(["--config", str(missing), "check"]) == 2
    err = capsys.readouterr().err
    assert str(missing) in err
    assert len(err.strip().splitlines()) == 1


def test_default_config_is_registry_sync_yaml_in_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["check"]) == 2
    assert "registry-sync.yaml" in capsys.readouterr().err


def test_missing_bucket_key_names_the_key(tmp_path):
    data = _valid()
    del data["buckets"]["Prospects"]
    with pytest.raises(ConfigError, match="Prospects"):
        load_config(_write(tmp_path / "registry-sync.yaml", data))


def test_missing_bucket_key_exits_2_with_one_line(tmp_path, capsys):
    data = _valid()
    del data["buckets"]["POCs"]
    path = _write(tmp_path / "registry-sync.yaml", data)
    assert cli.main(["--config", str(path), "check"]) == 2
    err = capsys.readouterr().err
    assert "POCs" in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("key", ["hub_page_id", "buckets"])
def test_missing_top_level_key_names_the_key(tmp_path, key):
    data = _valid()
    del data[key]
    with pytest.raises(ConfigError, match=key):
        load_config(_write(tmp_path / "registry-sync.yaml", data))


def test_unknown_bucket_name_is_rejected(tmp_path):
    data = _valid()
    data["buckets"]["Leads"] = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(ConfigError, match="Leads"):
        load_config(_write(tmp_path / "registry-sync.yaml", data))


def test_non_mapping_config_is_rejected(tmp_path):
    p = tmp_path / "registry-sync.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert set(cfg.buckets) == set(BUCKET_NAMES)
    # The example carries the workflow settings the reusable workflow reads.
    assert cfg.workflow is not None
    assert cfg.workflow.deploy_environment


def _workflow() -> dict:
    return {
        "deploy_environment": "registry-deploy",
        "readonly_environment": "registry-readonly",
    }


def test_workflow_settings_load(tmp_path):
    data = _valid()
    data["workflow"] = _workflow()
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.workflow == WorkflowConfig(
        deploy_environment="registry-deploy",
        readonly_environment="registry-readonly",
    )


def test_retired_secret_name_keys_are_ignored(tmp_path):
    """Secrets are passed by the caller now; old secret-name keys neither fail nor count."""
    data = _valid()
    data["workflow"] = {**_workflow(), "app_id_secret": "OLD_NAME"}
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.workflow == WorkflowConfig("registry-deploy", "registry-readonly")
    with pytest.raises(ConfigError, match=r"unknown config key 'workflow\.app_id_secret'"):
        config_value(cfg, "workflow.app_id_secret")


def test_config_without_workflow_still_loads(tmp_path):
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", _valid()))
    assert cfg.workflow is None
    with pytest.raises(ConfigError, match=r"missing required key 'workflow\.deploy_environment'"):
        config_value(cfg, "workflow.deploy_environment")


def test_partial_workflow_loads_and_names_the_missing_key(tmp_path):
    data = _valid()
    data["workflow"] = {k: v for k, v in _workflow().items() if k != "readonly_environment"}
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.workflow is None
    assert config_value(cfg, "workflow.deploy_environment") == "registry-deploy"
    with pytest.raises(ConfigError, match=r"'workflow\.readonly_environment'"):
        config_value(cfg, "workflow.readonly_environment")


@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 3])
def test_non_mapping_workflow_names_the_key_only_when_read(tmp_path, bad):
    data = _valid()
    data["workflow"] = bad
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert cfg.workflow is None
    with pytest.raises(ConfigError, match=r"'workflow\.deploy_environment'"):
        config_value(cfg, "workflow.deploy_environment")


def test_config_value_reads_top_level_and_defaults(tmp_path):
    data = _valid()
    data["workflow"] = _workflow()
    cfg = load_config(_write(tmp_path / "registry-sync.yaml", data))
    assert config_value(cfg, "data_dir") == str(tmp_path / "registry-data")
    assert config_value(cfg, "hub_page_id") == "11111111-1111-4111-8111-111111111111"
    assert config_value(cfg, "workflow.readonly_environment") == "registry-readonly"
    with pytest.raises(ConfigError, match=r"unknown config key 'workflow\.nope'"):
        config_value(cfg, "workflow.nope")
    with pytest.raises(ConfigError, match=r"unknown config key 'nope'"):
        config_value(cfg, "nope")


def test_cli_config_get(tmp_path, capsys):
    data = _valid()
    data["workflow"] = _workflow()
    path = _write(tmp_path / "registry-sync.yaml", data)
    assert cli.main(["--config", str(path), "config-get", "workflow.deploy_environment"]) == 0
    assert capsys.readouterr().out == "registry-deploy\n"
    assert cli.main(["--config", str(path), "config-get", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
