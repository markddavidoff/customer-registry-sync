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
import re
from pathlib import Path

import yaml

from .datasets import NOTION, DatasetRecord, dataset_dir
from .linear_schema import LinearCustomer
from .manifest import MANIFEST
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


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def slug_for(rec: CustomerRecord) -> str:
    if rec.domains:
        return _slugify(rec.domains[0].split(".")[0])
    return _slugify(rec.canonical_name)


def _unique_slugs(records: list[CustomerRecord]) -> dict[int, str]:
    seen: dict[str, int] = {}
    out: dict[int, str] = {}
    for rec in records:
        base = slug_for(rec) or "customer"
        if base in seen:
            seen[base] += 1
            out[id(rec)] = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
            out[id(rec)] = base
    return out


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
    slugs = _unique_slugs(ordered)
    return [(slugs[id(r)], r) for r in ordered]


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
    out = Path(out_dir)
    directory = dataset_dir(out, dataset)
    directory.mkdir(parents=True, exist_ok=True)

    if stems is None:
        files = dataset_files(records)
    else:
        if len(stems) != len(records):
            raise ValueError(f"stems has {len(stems)} entries for {len(records)} records")
        files = sorted(
            zip(stems, records, strict=True), key=lambda p: p[1].canonical_name.lower()
        )

    # remove stale generated yamls (never touches curated inputs outside the dataset directory)
    wanted = {f"{stem}.yaml" for stem, _ in files}
    for existing in directory.glob("*.yaml"):
        if existing.name not in wanted and existing.name != MANIFEST:
            existing.unlink()

    for stem, rec in files:
        text = yaml.safe_dump(
            rec.to_dict(), sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        (directory / f"{stem}.yaml").write_text(text)


# ---- Layer-1 dataset reader + Layer-2 desired/state read/write ----

DESIRED_SUBDIR = "linear"  # desired config (generate) — no linear_synced
STATE_SUBDIR = "linear-state"  # mirror of live Linear (sync-inbound) — with linear_synced


def load_dataset(data_dir: str | Path, dataset: str = NOTION) -> list[CustomerRecord]:
    """Read one dataset (<data_dir>/datasets/<name>/*.yaml) back into records."""
    ds = dataset_dir(Path(data_dir), dataset)
    out: list[CustomerRecord] = []
    for path in sorted(ds.glob("*.yaml")):
        if path.name == MANIFEST:
            continue
        out.append(CustomerRecord.from_dict(yaml.safe_load(path.read_text())))
    return out


def _layer2_slug(rec: LinearCustomer) -> str:
    if rec.external_ids:
        return _slugify(rec.external_ids[0])
    return _slugify(rec.name) or "customer"


def _load_layer2_dir(path: Path) -> list[LinearCustomer]:
    out: list[LinearCustomer] = []
    for p in sorted(path.glob("*.yaml")):
        if p.name == MANIFEST:
            continue
        out.append(LinearCustomer.from_dict(yaml.safe_load(p.read_text())))
    return out


def _write_layer2_dir(records: list[LinearCustomer], path: Path, *, strip_synced: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda r: r.name.lower())
    seen: dict[str, int] = {}
    slugs: dict[int, str] = {}
    wanted: set[str] = set()
    for rec in records:
        base = _layer2_slug(rec)
        if base in seen:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
            slug = base
        slugs[id(rec)] = slug
        wanted.add(f"{slug}.yaml")
    for existing in path.glob("*.yaml"):
        if existing.name not in wanted and existing.name != MANIFEST:
            existing.unlink()
    for rec in records:
        d = rec.to_dict()
        if strip_synced:
            d.pop("linear_synced", None)
        text = yaml.safe_dump(d, sort_keys=False, default_flow_style=False, allow_unicode=True)
        (path / f"{slugs[id(rec)]}.yaml").write_text(text)


def load_desired(data_dir: str | Path) -> list[LinearCustomer]:
    """<data_dir>/linear/*.yaml — the desired config a PR changes."""
    return _load_layer2_dir(Path(data_dir) / DESIRED_SUBDIR)


def write_desired(records: list[LinearCustomer], data_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(data_dir) / DESIRED_SUBDIR, strip_synced=True)


def load_state(data_dir: str | Path) -> list[LinearCustomer]:
    """<data_dir>/linear-state/*.yaml — the last-synced mirror of live Linear."""
    return _load_layer2_dir(Path(data_dir) / STATE_SUBDIR)


def write_state(records: list[LinearCustomer], data_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(data_dir) / STATE_SUBDIR, strip_synced=False)
