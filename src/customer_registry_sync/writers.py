"""Deterministic writers: one YAML per dataset record + two CSV rollups.

Output is stable across re-runs (sorted records, fixed key order, no
timestamps) so an unchanged upstream produces a zero-diff re-run. Only the
generated artifacts are written — curated inputs (self-managed-customers.yaml,
classification-overrides.yaml) are never touched. `write_dataset` (ingest)
writes dataset YAML only; the CSVs come from `generate` via `write_csvs`.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from gitops_engine.datasets import DatasetRecord, dataset_dir
from gitops_engine.writers import (
    load_record_dir,
    slugify,
    unique_slugs,
    write_files,
    write_record_dir,
)

from .datasets import NOTION
from .linear_schema import LinearCustomer
from .schema import CustomerRecord

REGISTRY_CSV = "registry.csv"
REAL_CUSTOMERS_CSV = "real-customers.csv"

REAL_CLASSIFICATIONS = frozenset({"real", "partner"})

CSV_COLUMNS = [
    "canonical_name",
    "domains",
    "buckets",
    "classification",
    "deployment",
    "arr",
    "tier",
    "status",
    "owner",
    "contact",
    "prod_org_id",
    "hubspot_id",
    "linear_customer_id",
    "sources",
    "notes",
    "dataset",
]


def slug_for(rec: CustomerRecord) -> str:
    if rec.domains:
        return slugify(rec.domains[0].split(".")[0])
    return slugify(rec.canonical_name)


def _record_slug(rec: CustomerRecord) -> str:
    return slug_for(rec) or "customer"


def _csv_row(entry: DatasetRecord) -> dict[str, str]:
    d = entry.record.to_dict()
    d["dataset"] = entry.dataset
    row: dict[str, str] = {}
    for col in CSV_COLUMNS:
        val = d.get(col)
        if isinstance(val, list):
            row[col] = "; ".join(val)
        elif val is None:
            row[col] = ""
        else:
            row[col] = str(val)
    return row


def csv_text(entries: list[DatasetRecord]) -> str:
    """One CSV rollup of `entries`, sorted by lower-cased name; the exact file text."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for entry in sorted(entries, key=lambda e: e.record.canonical_name.lower()):
        w.writerow(_csv_row(entry))
    return buf.getvalue()


def real_entries(entries: list[DatasetRecord]) -> list[DatasetRecord]:
    """The `real-customers.csv` subset: stubs included, by their Linear-status classification."""
    return [e for e in entries if e.record.classification in REAL_CLASSIFICATIONS]


def write_csvs(entries: list[DatasetRecord], out_dir: str | Path) -> None:
    """`registry.csv` (every entry) and `real-customers.csv` (real + partner).

    `generate` passes its merged winners; nothing else writes these files."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / REGISTRY_CSV).write_bytes(csv_text(entries).encode("utf-8"))
    (out / REAL_CUSTOMERS_CSV).write_bytes(csv_text(real_entries(entries)).encode("utf-8"))


def dataset_files(records: list[CustomerRecord]) -> list[tuple[str, CustomerRecord]]:
    """(file stem, record) in write order: sorted by name, slugs made unique."""
    ordered = sorted(records, key=lambda r: r.canonical_name.lower())
    return list(zip(unique_slugs([_record_slug(r) for r in ordered]), ordered, strict=True))


def write_dataset(
    records: list[CustomerRecord],
    out_dir: str | Path,
    *,
    dataset: str = NOTION,
    stems: list[str] | None = None,
) -> None:
    """Write one dataset's YAML files to ``<out_dir>/datasets/<dataset>/``.

    ``stems`` (parallel to ``records``) names the files; the default is `dataset_files`.
    The CSVs are not written here: they come from `generate`'s merged result (`write_csvs`).
    """
    directory = dataset_dir(Path(out_dir), dataset)
    directory.mkdir(parents=True, exist_ok=True)

    if stems is None:
        files = dataset_files(records)
    else:
        if len(stems) != len(records):
            raise ValueError(f"stems has {len(stems)} entries for {len(records)} records")
        files = sorted(zip(stems, records, strict=True), key=lambda p: p[1].canonical_name.lower())

    # stale generated yamls go; curated inputs outside the dataset directory are never touched
    write_files(files, directory, to_dict=CustomerRecord.to_dict)


# ---- Layer-1 dataset reader + Layer-2 desired/state read/write ----

DESIRED_SUBDIR = "linear"  # desired config (generate) — no linear_synced
STATE_SUBDIR = "linear-state"  # mirror of live Linear (sync-inbound) — with linear_synced


def load_dataset(data_dir: str | Path, dataset: str = NOTION) -> list[CustomerRecord]:
    """Read one dataset (<data_dir>/datasets/<name>/*.yaml) back into records."""
    return load_record_dir(dataset_dir(Path(data_dir), dataset), CustomerRecord.from_dict)


def _layer2_slug(rec: LinearCustomer) -> str:
    if rec.external_ids:
        return slugify(rec.external_ids[0])
    return slugify(rec.name) or "customer"


def _write_layer2_dir(records: list[LinearCustomer], path: Path, *, strip_synced: bool) -> None:
    def to_dict(rec: LinearCustomer) -> dict:
        d = rec.to_dict()
        if strip_synced:
            d.pop("linear_synced", None)
        return d

    rows = sorted(records, key=lambda r: r.name.lower())
    write_record_dir(rows, path, slug=_layer2_slug, to_dict=to_dict)


def load_desired(data_dir: str | Path) -> list[LinearCustomer]:
    """<data_dir>/linear/*.yaml — the desired config a PR changes."""
    return load_record_dir(Path(data_dir) / DESIRED_SUBDIR, LinearCustomer.from_dict)


def write_desired(records: list[LinearCustomer], data_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(data_dir) / DESIRED_SUBDIR, strip_synced=True)


def load_state(data_dir: str | Path) -> list[LinearCustomer]:
    """<data_dir>/linear-state/*.yaml — the last-synced mirror of live Linear."""
    return load_record_dir(Path(data_dir) / STATE_SUBDIR, LinearCustomer.from_dict)


def write_state(records: list[LinearCustomer], data_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(data_dir) / STATE_SUBDIR, strip_synced=False)
