"""Deterministic writers: one YAML per customer + two CSV rollups.

Output is stable across re-runs (sorted records, fixed key order, no
timestamps) so an unchanged upstream produces a zero-diff re-run. Only the
generated artifacts are written — curated inputs (self-managed-customers.yaml,
classification-overrides.yaml) are never touched.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import yaml

from .linear_schema import LinearCustomer
from .schema import CustomerRecord

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


def _csv_row(rec: CustomerRecord) -> dict[str, str]:
    d = rec.to_dict()
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


def _write_csv(path: Path, records: list[CustomerRecord]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for rec in records:
            w.writerow(_csv_row(rec))


def write_dataset(records: list[CustomerRecord], out_dir: str | Path) -> None:
    out = Path(out_dir)
    dataset = out / "dataset"
    dataset.mkdir(parents=True, exist_ok=True)

    records = sorted(records, key=lambda r: r.canonical_name.lower())
    slugs = _unique_slugs(records)

    # remove stale generated yamls (never touches curated inputs outside dataset/)
    wanted = {f"{slugs[id(r)]}.yaml" for r in records}
    for existing in dataset.glob("*.yaml"):
        if existing.name not in wanted:
            existing.unlink()

    for rec in records:
        path = dataset / f"{slugs[id(rec)]}.yaml"
        text = yaml.safe_dump(
            rec.to_dict(), sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        path.write_text(text)

    _write_csv(out / "registry.csv", records)
    real = [r for r in records if r.classification in REAL_CLASSIFICATIONS]
    _write_csv(out / "real-customers.csv", real)


# ---- Layer-1 dataset reader + Layer-2 desired/state read/write ----

DESIRED_SUBDIR = "linear"  # desired config (generate) — no linear_synced
STATE_SUBDIR = "linear-state"  # mirror of live Linear (sync-inbound) — with linear_synced


def load_dataset(customers_dir: str | Path) -> list[CustomerRecord]:
    """Read the Layer-1 dataset (customers/dataset/*.yaml) back into records."""
    ds = Path(customers_dir) / "dataset"
    out: list[CustomerRecord] = []
    for path in sorted(ds.glob("*.yaml")):
        out.append(CustomerRecord.from_dict(yaml.safe_load(path.read_text())))
    return out


def _layer2_slug(rec: LinearCustomer) -> str:
    if rec.external_ids:
        return _slugify(rec.external_ids[0])
    return _slugify(rec.name) or "customer"


def _load_layer2_dir(path: Path) -> list[LinearCustomer]:
    out: list[LinearCustomer] = []
    for p in sorted(path.glob("*.yaml")):
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
        if existing.name not in wanted:
            existing.unlink()
    for rec in records:
        d = rec.to_dict()
        if strip_synced:
            d.pop("linear_synced", None)
        text = yaml.safe_dump(d, sort_keys=False, default_flow_style=False, allow_unicode=True)
        (path / f"{slugs[id(rec)]}.yaml").write_text(text)


def load_desired(customers_dir: str | Path) -> list[LinearCustomer]:
    """customers/linear/*.yaml — the desired config a PR changes."""
    return _load_layer2_dir(Path(customers_dir) / DESIRED_SUBDIR)


def write_desired(records: list[LinearCustomer], customers_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(customers_dir) / DESIRED_SUBDIR, strip_synced=True)


def load_state(customers_dir: str | Path) -> list[LinearCustomer]:
    """customers/linear-state/*.yaml — the last-synced mirror of live Linear."""
    return _load_layer2_dir(Path(customers_dir) / STATE_SUBDIR)


def write_state(records: list[LinearCustomer], customers_dir: str | Path) -> None:
    _write_layer2_dir(records, Path(customers_dir) / STATE_SUBDIR, strip_synced=False)
