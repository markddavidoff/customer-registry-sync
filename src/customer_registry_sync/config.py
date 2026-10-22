"""Registry configuration: the Notion page ids and the data directory.

Loaded from a YAML file (default ``registry-sync.yaml`` in the working
directory; override with the global ``--config`` flag). See
``examples/registry-sync.example.yaml``.

Keys:

- ``hub_page_id`` (required) — the hub page that contains the bucket pages.
- ``buckets`` (required) — bucket name -> Notion page id. Every bucket name the
  ingest knows must be present (see ``notion_ingest.BUCKET_CLASSIFICATION``).
- ``data_dir`` (optional, default ``customers``) — where the dataset lives. A
  relative path resolves against the directory that holds the config file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .notion_ingest import BUCKET_CLASSIFICATION

DEFAULT_CONFIG_NAME = "registry-sync.yaml"
DEFAULT_DATA_DIR = "customers"


class ConfigError(Exception):
    """The config file is missing, unreadable, or lacks a required key."""


@dataclass
class RegistryConfig:
    hub_page_id: str
    buckets: dict[str, str] = field(default_factory=dict)
    data_dir: Path = Path(DEFAULT_DATA_DIR)


def _require_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config is missing required key '{label}'")
    return value.strip()


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

    hub_page_id = _require_str(data, "hub_page_id", "hub_page_id")

    raw_buckets = data.get("buckets")
    if not isinstance(raw_buckets, dict):
        raise ConfigError("config is missing required key 'buckets'")
    for name in raw_buckets:
        if name not in BUCKET_CLASSIFICATION:
            raise ConfigError(f"config has unknown bucket '{name}' under 'buckets'")
    buckets = {
        name: _require_str(raw_buckets, name, f"buckets.{name}") for name in BUCKET_CLASSIFICATION
    }

    data_dir = Path(str(data.get("data_dir") or DEFAULT_DATA_DIR))
    if not data_dir.is_absolute():
        data_dir = path.absolute().parent / data_dir
    return RegistryConfig(
        hub_page_id=hub_page_id,
        buckets=buckets,
        data_dir=data_dir,
    )
