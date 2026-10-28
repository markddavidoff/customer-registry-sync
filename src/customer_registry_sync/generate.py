"""Generate: merge the datasets by identity and project them onto Linear's schema.

`merge_datasets` reads every dataset record, resolves each to its identity
(`identity.resolve`), and keeps one record per identity: the one from the
earliest dataset in priority order, used whole. The winner gains the
identity's extra domains. Each winner is projected to a `LinearCustomer`
(Linear-supported fields only): classification→status, arr→revenue(int), tier
passthrough, and `external_ids` carrying the identity's canonical id as the
stable dataset↔Linear link. `internal` records are not projected. A
`linear-found` winner becomes a `linear_owned` row whose name keeps its Linear
form. Produces desired config only; `linear_synced` is never emitted (state
lives in `<data_dir>/linear-state/`).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from .datasets import LINEAR_FOUND, DatasetRecord, load_datasets, load_datasets_config
from .identity import (  # noqa: F401 (re-export)
    _STATUS_SUFFIX_RE,
    IDENTITIES_FILE,
    IdentityTable,
    keys_for_record,
    load_identities,
    resolve,
    strip_status_suffix,
)
from .linear_schema import LinearCustomer
from .schema import CustomerRecord
from .writers import slug_for

# classification → Linear status. None = not projected to Linear.
STATUS_MAP: dict[str | None, str | None] = {
    "real": "Active",
    "partner": "Active",
    "prospect": "Prospect",
    "churned": "Churned",
    "internal": None,
    None: None,
}


def project_record(
    rec: CustomerRecord,
    *,
    external_id: str,
    linear_owned: bool = False,
) -> LinearCustomer | None:
    """One record as a desired Linear row; None for records Linear does not get.

    A linear-owned (stub) row keeps `canonical_name` as is: no status suffix."""
    status = STATUS_MAP.get(rec.classification)
    if status is None:
        return None  # internal / unclassified → not a Linear customer
    return LinearCustomer(
        name=rec.canonical_name if linear_owned else f"{rec.canonical_name} [{status}]",
        domains=list(rec.domains),
        revenue=int(round(rec.arr)) if rec.arr is not None else None,
        tier=rec.tier,
        status=status,
        external_ids=[external_id],
        linear_owned=linear_owned,
    )


def generate_linear(dataset_recs: list[CustomerRecord]) -> list[LinearCustomer]:
    """Project records with no identity table: the external id is `slug_for(rec)`.
    Desired only — no sync state; state lives in `<data_dir>/linear-state/`."""
    out = [
        row
        for rec in dataset_recs
        if (row := project_record(rec, external_id=slug_for(rec))) is not None
    ]
    out.sort(key=lambda x: x.name.lower())
    return out


@dataclass
class Generated:
    rows: list[LinearCustomer]  # desired rows for linear/, sorted by name
    winners: list[DatasetRecord]  # one per identity, identity domains added; the CSV input


def canonical_id_for(entry: DatasetRecord, table: IdentityTable) -> str:
    """The identity an entry resolves to. One that resolves to none keeps a stable id:
    a `linear-found` stub its file stem (the id already stamped into Linear), any other
    record `slug_for`."""
    keys = keys_for_record(entry.record, dataset=entry.dataset, source_id=entry.source_id)
    hit = resolve(keys, table)
    if hit is not None:
        return hit.canonical_id
    return entry.source_id if entry.dataset == LINEAR_FOUND else slug_for(entry.record)


def group_by_identity(
    entries: list[DatasetRecord],
    table: IdentityTable,
    priority: list[str],
) -> dict[str, list[DatasetRecord]]:
    """Canonical id -> its entries, best first: dataset priority, then file name."""
    rank = {name: i for i, name in enumerate(priority)}
    groups: dict[str, list[DatasetRecord]] = {}
    for entry in entries:
        groups.setdefault(canonical_id_for(entry, table), []).append(entry)
    for group in groups.values():
        group.sort(key=lambda e: (rank.get(e.dataset, len(rank)), e.source_id))
    return groups


def _with_identity_domains(rec: CustomerRecord, extra: list[str]) -> CustomerRecord:
    """Record domains first, then identity domains not already present (case-insensitive)."""
    domains = list(rec.domains)
    seen = {d.lower() for d in domains}
    for d in extra:
        if d.lower() not in seen:
            seen.add(d.lower())
            domains.append(d)
    return dataclasses.replace(rec, domains=domains) if domains != rec.domains else rec


def merge_datasets(
    entries: list[DatasetRecord],
    table: IdentityTable,
    priority: list[str],
) -> Generated:
    """One row per identity; the highest-priority record wins whole."""
    rows: list[LinearCustomer] = []
    winners: list[DatasetRecord] = []
    for cid, group in group_by_identity(entries, table, priority).items():
        best = group[0]
        ident = table.entries.get(cid)
        rec = _with_identity_domains(best.record, ident.values["domains"] if ident else [])
        winner = DatasetRecord(dataset=best.dataset, source_id=best.source_id, record=rec)
        winners.append(winner)
        row = project_record(rec, external_id=cid, linear_owned=best.dataset == LINEAR_FOUND)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda x: x.name.lower())
    winners.sort(key=lambda w: w.record.canonical_name.lower())
    return Generated(rows=rows, winners=winners)


def generate_from_dir(data_dir: Path) -> Generated:
    """`merge_datasets` over `<data_dir>/datasets/*` and `<data_dir>/identities.yaml`."""
    root = Path(data_dir)
    cfg = load_datasets_config(root)
    table = load_identities(root / IDENTITIES_FILE)
    return merge_datasets(load_datasets(root, cfg), table, cfg.names)
