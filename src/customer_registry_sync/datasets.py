"""`datasets.yaml` and the per-dataset directories under `datasets/`, both inside `data_dir`.

The file lists dataset names in priority order (earlier wins) and the Linear status
table `linear-found` uses to classify the stubs it builds. It is curated, so a missing
file is not an error: it means `[notion]` with the default table.
Each dataset directory holds `CustomerRecord` YAML files plus a `.manifest.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .manifest import MANIFEST
from .schema import VALID_CLASSIFICATIONS, CustomerRecord

DATASETS_FILE = "datasets.yaml"
DATASETS_SUBDIR = "datasets"
NOTION = "notion"
LINEAR_FOUND = "linear-found"
DEFAULT_STATUS_TABLE = {"Active": "real", "Prospect": "prospect", "Churned": "churned"}


@dataclass(frozen=True)
class DatasetConfig:
    names: list[str] = field(default_factory=lambda: [NOTION])
    status_classification: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_STATUS_TABLE)
    )


@dataclass(frozen=True)
class DatasetRecord:
    dataset: str
    source_id: str  # the file stem inside the dataset directory
    record: CustomerRecord


def _read(data_dir: Path) -> dict | None:
    path = Path(data_dir) / DATASETS_FILE
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text())
    return raw if isinstance(raw, dict) else {}


def load_datasets_config(data_dir: Path) -> DatasetConfig:
    """Parse `datasets.yaml`; the default when the file is absent.

    A present file with no `datasets` key yields an empty name list, which
    `datasets_errors` reports. A missing status table falls back to the default.
    """
    raw = _read(data_dir)
    if raw is None:
        return DatasetConfig()
    names = [str(n) for n in raw.get("datasets") or []]
    table = raw.get("linear_status_classification")
    status = (
        {str(k): v for k, v in table.items()}
        if isinstance(table, dict)
        else dict(DEFAULT_STATUS_TABLE)
    )
    return DatasetConfig(names=names, status_classification=status)


def dataset_dir(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / DATASETS_SUBDIR / name


def datasets_errors(data_dir: Path) -> list[str]:
    """Problems in `datasets.yaml` or the directories it should match; empty when clean."""
    root = Path(data_dir)
    cfg = load_datasets_config(root)
    errors: list[str] = []
    if not cfg.names:
        errors.append("datasets.yaml: empty datasets list")
    seen: set[str] = set()
    for name in cfg.names:
        if name in seen:
            errors.append(f"datasets.yaml: {name} listed twice")
            continue
        seen.add(name)
        if not dataset_dir(root, name).is_dir():
            errors.append(f"datasets.yaml: {name} has no directory")
    base = root / DATASETS_SUBDIR
    if base.is_dir():
        errors += [
            f"datasets.yaml: directory datasets/{p.name} is not listed"
            for p in sorted(base.iterdir())
            if p.is_dir() and p.name not in seen
        ]
    for status, classification in sorted(cfg.status_classification.items()):
        if classification is None or classification not in VALID_CLASSIFICATIONS:
            errors.append(
                f"datasets.yaml: status {status} maps to invalid classification {classification}"
            )
    return errors


def load_datasets(data_dir: Path, cfg: DatasetConfig) -> list[DatasetRecord]:
    """Every record of every configured dataset: priority order, then file name."""
    out: list[DatasetRecord] = []
    for name in cfg.names:
        directory = dataset_dir(data_dir, name)
        for path in sorted(directory.glob("*.yaml")):
            if path.name == MANIFEST:
                continue
            record = CustomerRecord.from_dict(yaml.safe_load(path.read_text()))
            out.append(DatasetRecord(dataset=name, source_id=path.stem, record=record))
    return out
