"""The customer parts of `datasets.yaml` and the dataset directories.

The directory format, priority order and structural checks live in
`gitops_engine.datasets`. This module adds the dataset names this tool uses and the
Linear status table `linear-found` uses to classify the stubs it builds. The file is
curated, so a missing one is not an error: it means `[notion]` with the default table.
Each dataset directory holds `CustomerRecord` YAML files plus a `.manifest.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine import datasets as engine_datasets
from gitops_engine.datasets import DatasetRecord

from .schema import VALID_CLASSIFICATIONS, CustomerRecord

NOTION = "notion"
LINEAR_FOUND = "linear-found"
DEFAULT_STATUS_TABLE = {"Active": "real", "Prospect": "prospect", "Churned": "churned"}
STATUS_TABLE_KEY = "linear_status_classification"

__all__ = [
    "DEFAULT_STATUS_TABLE",
    "LINEAR_FOUND",
    "NOTION",
    "DatasetConfig",
    "DatasetRecord",
    "datasets_errors",
    "load_datasets",
    "load_datasets_config",
]


@dataclass(frozen=True)
class DatasetConfig:
    names: list[str] = field(default_factory=lambda: [NOTION])
    status_classification: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_STATUS_TABLE)
    )


def load_datasets_config(data_dir: Path) -> DatasetConfig:
    """Parse `datasets.yaml`; the default when the file is absent.

    A present file with no `datasets` key yields an empty name list, which
    `datasets_errors` reports. A missing status table falls back to the default.
    """
    cfg = engine_datasets.load_datasets_config(data_dir, default_names=[NOTION])
    table = cfg.extra.get(STATUS_TABLE_KEY)
    status = (
        {str(k): v for k, v in table.items()}
        if isinstance(table, dict)
        else dict(DEFAULT_STATUS_TABLE)
    )
    return DatasetConfig(names=cfg.names, status_classification=status)


def datasets_errors(data_dir: Path) -> list[str]:
    """Problems in `datasets.yaml` or the directories it should match; empty when clean."""
    errors = engine_datasets.datasets_errors(data_dir, default_names=[NOTION])
    cfg = load_datasets_config(data_dir)
    for status, classification in sorted(cfg.status_classification.items()):
        if classification is None or classification not in VALID_CLASSIFICATIONS:
            errors.append(
                f"datasets.yaml: status {status} maps to invalid classification {classification}"
            )
    return errors


def load_datasets(data_dir: Path, cfg: DatasetConfig) -> list[DatasetRecord[CustomerRecord]]:
    """Every record of every configured dataset: priority order, then file name."""
    return engine_datasets.load_datasets(
        data_dir, engine_datasets.DatasetConfig(names=cfg.names), CustomerRecord.from_dict
    )
