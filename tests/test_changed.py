"""`changed --since`: does a push touch a deploy input? Throwaway git repos only."""

import subprocess
from pathlib import Path

import pytest
import yaml

from gitops_engine.changed import changed_since

from customer_registry_sync import cli
from customer_registry_sync.changed import deploy_inputs
from customer_registry_sync.config import load_config

BUCKET_NAMES = (
    "Customers",
    "Churned Customers",
    "Archive",
    "POCs",
    "Prospects",
    "Partner & Supporting Accounts",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_config(repo: Path, workflow: dict | None = None) -> Path:
    data = {
        "source": {
            "kind": "notion",
            "hub_page_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "buckets": {
                name: f"bbbbbbbb-bbbb-4bbb-8bbb-{i:012d}" for i, name in enumerate(BUCKET_NAMES)
            },
        },
        "data_dir": "registry",
    }
    if workflow is not None:
        data["workflow"] = workflow
    path = repo / "registry-sync.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "caller"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _write_config(root)
    data = root / "registry"
    for rel in (
        "identities.yaml",
        "linear-ignore.yaml",
        "datasets.yaml",
        "linear-retire.yaml",
        "linear/acme.yaml",
        "linear-state/acme.yaml",
        "linear-snapshots/20260101T000000Z.yaml",
        "datasets/notion/acme.yaml",
    ):
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed: 1\n")
    base = _commit_all(root, "seed")
    return root, base


def _edit(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((path.read_text() if path.exists() else "") + "edit: 1\n")


def test_deploy_inputs_are_repo_relative(repo):
    root, _ = repo
    cfg = load_config(root / "registry-sync.yaml")
    assert deploy_inputs(cfg, root) == [
        "registry/linear/**",
        "registry/identities.yaml",
        "registry/linear-ignore.yaml",
        "registry/datasets.yaml",
        "registry/linear-retire.yaml",
        "registry-sync.yaml",
    ]


def test_config_edit_is_changed(repo):
    """The config file moves data_dir and names the environments: a deploy input."""
    root, base = repo
    _edit(root / "registry-sync.yaml")
    _commit_all(root, "edit config")
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, base, deploy_inputs(cfg, root)) is True


def test_state_only_push_is_unchanged(repo):
    root, base = repo
    _edit(root / "registry" / "linear-state" / "acme.yaml")
    _edit(root / "registry" / "linear-state" / "new.yaml")
    _edit(root / "registry" / "linear-snapshots" / "20260102T000000Z.yaml")
    _commit_all(root, "state PR")
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, base, deploy_inputs(cfg, root)) is False


@pytest.mark.parametrize(
    "rel",
    [
        "identities.yaml",
        "datasets.yaml",
        "linear-ignore.yaml",
        "linear-retire.yaml",
        "linear/acme.yaml",
        "linear/new-row.yaml",
    ],
)
def test_curated_edit_is_changed(repo, rel):
    root, base = repo
    _edit(root / "registry" / rel)
    _commit_all(root, f"edit {rel}")
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, base, deploy_inputs(cfg, root)) is True


def test_deleted_desired_row_is_changed(repo):
    root, base = repo
    (root / "registry" / "linear" / "acme.yaml").unlink()
    _commit_all(root, "drop a row")
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, base, deploy_inputs(cfg, root)) is True


def test_lookalike_paths_are_not_inputs(repo):
    root, base = repo
    # `linear-state/` shares a prefix with `linear/`; a dataset folder is not a deploy input.
    _edit(root / "registry" / "linear-state" / "acme.yaml")
    _edit(root / "registry" / "datasets" / "notion" / "acme.yaml")
    _edit(root / "registry" / "identities.yaml.bak")
    _commit_all(root, "lookalikes")
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, base, deploy_inputs(cfg, root)) is False


@pytest.mark.parametrize("sha", ["0" * 40, "deadbeef" * 5, "not-a-sha", "", "--output=/dev/null"])
def test_unknown_sha_is_changed(repo, sha):
    root, _ = repo
    cfg = load_config(root / "registry-sync.yaml")
    assert changed_since(root, sha, deploy_inputs(cfg, root)) is True


def test_data_dir_outside_the_repo_is_changed(repo, tmp_path, capsys, monkeypatch):
    root, base = repo
    cfg = load_config(root / "registry-sync.yaml")
    cfg.data_dir = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="outside the repository"):
        deploy_inputs(cfg, root)
    # The CLI treats any doubt as "changed".
    data = yaml.safe_load((root / "registry-sync.yaml").read_text())
    data["data_dir"] = str(tmp_path / "elsewhere")
    (root / "registry-sync.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    monkeypatch.chdir(root)
    assert cli.main(["changed", "--since", base]) == 0
    assert capsys.readouterr().out.strip() == "changed"


def test_cli_changed_prints_and_exits_zero(repo, capsys, monkeypatch):
    root, base = repo
    monkeypatch.chdir(root)
    assert cli.main(["changed", "--since", base]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    _edit(root / "registry" / "identities.yaml")
    _commit_all(root, "edit identities")
    assert cli.main(["changed", "--since", base]) == 0
    assert capsys.readouterr().out.strip() == "changed"

    assert cli.main(["changed", "--since", "0" * 40]) == 0
    assert capsys.readouterr().out.strip() == "changed"


def test_cli_changed_from_a_subdirectory(repo, capsys, monkeypatch):
    root, base = repo
    _edit(root / "registry" / "linear-retire.yaml")
    _commit_all(root, "edit retire")
    monkeypatch.chdir(root / "registry")
    assert cli.main(["--config", str(root / "registry-sync.yaml"), "changed", "--since", base]) == 0
    assert capsys.readouterr().out.strip() == "changed"


def test_workflow_keys_missing(repo, capsys, monkeypatch):
    """No `workflow:` mapping: the config loads and `changed` works; `config-get` names the key."""
    root, base = repo
    monkeypatch.chdir(root)
    cfg = load_config(root / "registry-sync.yaml")
    assert cfg.workflow is None
    assert cli.main(["changed", "--since", base]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    assert cli.main(["config-get", "workflow.deploy_environment"]) == 2
    err = capsys.readouterr().err
    assert "workflow.deploy_environment" in err

    # A partial mapping names exactly the key that is missing.
    _write_config(root, {"deploy_environment": "deploy-env"})
    assert cli.main(["config-get", "workflow.deploy_environment"]) == 0
    assert capsys.readouterr().out.strip() == "deploy-env"
    assert cli.main(["config-get", "workflow.readonly_environment"]) == 2
    assert "workflow.readonly_environment" in capsys.readouterr().err
    assert cli.main(["changed", "--since", base]) == 0
