"""Registry configuration: the source adapter and the data directory.

Loaded from a YAML file (default ``registry-sync.yaml`` in the working
directory; override with the global ``--config`` flag). See
``examples/registry-sync.example.yaml``.

Keys:

- ``source`` (required) — the source adapter, picked by ``kind``. The other keys
  belong to that adapter (``sources.SOURCES`` lists the kinds). For ``kind: notion``:

  - ``hub_page_id`` (required) — the hub page that contains the bucket pages.
  - ``buckets`` (required) — bucket name -> Notion page id. Every bucket name the
    ingest knows must be present (see ``sources.notion.BUCKET_CLASSIFICATION``).

  An unknown ``kind`` is an error that names the known kinds. ``hub_page_id`` or
  ``buckets`` at the top level (the v0.1 shape) is an error that points to ``source:``.
- ``data_dir`` (optional, default ``customers``) — where the dataset lives. A
  relative path resolves against the directory that holds the config file.
- ``workflow`` (optional) — settings the gitops-engine reusable sync workflow reads
  through ``config-get``: ``deploy_environment`` and ``readonly_environment``, the
  names of the two GitHub environments. Secrets are not named here: the caller
  passes them to the workflow explicitly. A config without these keys still loads;
  only ``config-get`` of a missing key fails, naming it. Unknown keys under
  ``workflow:`` are ignored.

``config-get`` answers the engine CLI contract's keys (``data_dir``,
``deploy_environment``, ``readonly_environment``, ``state_dir``, ``snapshot_dir``,
``e2e_tests``); ``workflow.deploy_environment`` and ``workflow.readonly_environment``
are accepted as the same keys. ``source.kind`` names the selected source adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .linear_files import SNAPSHOT_SUBDIR
from .sources import SOURCES, SourceAdapter, SourceConfigError
from .writers import STATE_SUBDIR

DEFAULT_CONFIG_NAME = "registry-sync.yaml"
DEFAULT_DATA_DIR = "customers"
# The live GitOps tests, relative to the root of this tool's source (`config-get e2e_tests`).
E2E_TESTS = "tests/test_e2e_gitops.py"


class ConfigError(Exception):
    """The config file is missing, unreadable, or lacks a required key."""


@dataclass(frozen=True)
class WorkflowConfig:
    """The GitHub environments the reusable workflow binds its jobs to."""

    deploy_environment: str
    readonly_environment: str


WORKFLOW_KEYS: tuple[str, ...] = tuple(f.name for f in fields(WorkflowConfig))


@dataclass
class RegistryConfig:
    source: SourceAdapter
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    # Complete `workflow:` settings, or None. `workflow_raw` keeps a partial mapping so
    # `config_value` can name the one key that is missing.
    workflow: WorkflowConfig | None = None
    workflow_raw: dict[str, str] = field(default_factory=dict)
    # The file this config was loaded from; `changed` treats it as a deploy input.
    config_path: Path | None = None


def _workflow_raw(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("workflow")
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v).strip()
        for k, v in raw.items()
        if isinstance(v, str | int) and not isinstance(v, bool) and str(v).strip()
    }


def _workflow(raw: dict[str, str]) -> WorkflowConfig | None:
    values = {k: v for k, v in raw.items() if k in WORKFLOW_KEYS}
    if any(k not in values for k in WORKFLOW_KEYS):
        return None
    return WorkflowConfig(**values)


def config_value(cfg: RegistryConfig, key: str) -> str:
    """One config value by key: the engine CLI contract's keys, plus ``source.kind``.

    Raises ``ConfigError`` naming the key when it is unknown or missing. A missing
    environment names its place in the file (``workflow.<key>``).
    """
    if key == "data_dir":
        return str(cfg.data_dir)
    if key == "state_dir":
        return str(cfg.data_dir / STATE_SUBDIR)
    if key == "snapshot_dir":
        return str(cfg.data_dir / SNAPSHOT_SUBDIR)
    if key == "e2e_tests":
        return E2E_TESTS
    if key in WORKFLOW_KEYS:
        key = f"workflow.{key}"
    if key == "source.kind":
        return cfg.source.name
    if key.startswith("workflow."):
        name = key.removeprefix("workflow.")
        if name in WORKFLOW_KEYS:
            value = cfg.workflow_raw.get(name)
            if not value:
                raise ConfigError(f"config is missing required key '{key}'")
            return value
    raise ConfigError(f"unknown config key '{key}'")


# Keys the v0.1 file carried at the top level; the source adapter owns them now.
_MOVED_TO_SOURCE = ("hub_page_id", "buckets")


def _load_source(data: dict[str, Any]) -> SourceAdapter:
    known = ", ".join(sorted(SOURCES))
    for key in _MOVED_TO_SOURCE:
        if key in data:
            raise ConfigError(
                f"config key '{key}' moved under 'source:' "
                "(source: {kind: notion, hub_page_id: ..., buckets: {...}}); see "
                "examples/registry-sync.example.yaml"
            )
    section = data.get("source")
    if not isinstance(section, dict):
        raise ConfigError(f"config is missing required key 'source' (a mapping with kind: {known})")
    kind = section.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ConfigError(f"config is missing required key 'source.kind' (known kinds: {known})")
    adapter = SOURCES.get(kind.strip())
    if adapter is None:
        raise ConfigError(f"unknown source kind '{kind}' (known kinds: {known})")
    try:
        return adapter.from_config(section)
    except SourceConfigError as e:
        raise ConfigError(str(e)) from None


def load_config(path: Path) -> RegistryConfig:
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e.strerror or e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ConfigError(f"config file {path} is not valid YAML") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a YAML mapping")

    source = _load_source(data)

    data_dir = Path(str(data.get("data_dir") or DEFAULT_DATA_DIR))
    if not data_dir.is_absolute():
        data_dir = path.absolute().parent / data_dir
    workflow_raw = _workflow_raw(data)
    return RegistryConfig(
        source=source,
        data_dir=data_dir,
        workflow=_workflow(workflow_raw),
        workflow_raw=workflow_raw,
        config_path=path.absolute(),
    )
