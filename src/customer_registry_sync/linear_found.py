"""The `linear-found` dataset: a stub for each Linear customer the Notion dataset lacks.

`ingest` builds it offline from the state mirror. Each managed (non-ignored) Linear
customer is resolved against the identity table. One that resolves to nothing gets a
new identity first; one whose identity has no `notion` record becomes a stub. Every
primary customer's Linear-only domains and its Linear id are appended to its identity.
A stub's file stem is its canonical id, and a stub keeps that stem across runs, keyed
by its Linear id, so the external id stamped into Linear never drifts when the
customer's name or domain changes there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .datasets import LINEAR_FOUND, NOTION, DatasetConfig, DatasetRecord
from .deploy import pick_primary, stamped_conflict
from .identity import (
    SOURCE_LINEAR,
    SOURCE_NOTION,
    ItemKeys,
    keys_for_live,
    keys_for_record,
    resolve,
    strip_status_suffix,
)
from .identity_store import IdentityStore
from .linear_client import LiveCustomer
from .schema import VALID_CLASSIFICATIONS, CustomerRecord
from .writers import slug_for

STUB_NOTE = "Found in Linear {date}; no Notion record."


def append_record_identities(store: IdentityStore, entries: list[DatasetRecord]) -> None:
    """Give every `notion` record an identity, or append its missing values."""
    for entry in entries:
        if entry.dataset != NOTION:
            continue
        rec = entry.record
        keys = keys_for_record(rec, dataset=NOTION, source_id=entry.source_id)
        values = {
            "names": [rec.canonical_name],
            "domains": list(rec.domains),
            "sources": {SOURCE_NOTION: entry.source_id},
        }
        hit = resolve(keys, store.table)
        if hit is None:
            store.add_identity(slug_for(rec) or "customer", **values)
        else:
            store.add_values(hit.canonical_id, **values)


@dataclass
class StubBuild:
    stubs: list[tuple[str, CustomerRecord]] = field(default_factory=list)  # (canonical id, record)
    duplicates: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)


def _classification(cfg: DatasetConfig, status: str | None) -> str | None:
    """The table's class for a Linear status; None when unmapped (`check` fails)."""
    value = cfg.status_classification.get(status or "")
    return value if value is not None and value in VALID_CLASSIFICATIONS else None


def _stub(lv: LiveCustomer, cfg: DatasetConfig, note: str) -> CustomerRecord:
    return CustomerRecord(
        canonical_name=strip_status_suffix(lv.name),
        domains=list(lv.domains),
        arr=float(lv.revenue) if lv.revenue is not None else None,
        tier=lv.tier,
        status=lv.status or None,
        classification=_classification(cfg, lv.status),  # type: ignore[arg-type]
        linear_customer_id=lv.id,
        sources=[f"linear:{lv.id}"],
        notes=note,
    )


def build_linear_found(
    managed: list[LiveCustomer],
    store: IdentityStore,
    entries: list[DatasetRecord],
    cfg: DatasetConfig,
    *,
    today: date,
) -> StubBuild:
    """Stubs for the managed customers whose identity has no `notion` record.

    `managed` must already exclude ignored customers. Customers are taken in Linear-id
    order. Of several on one identity, the primary is the one carrying the canonical id
    or the recorded Linear id (as the planner picks it); the others are known duplicates
    and contribute nothing. When neither id is on any of them, nothing is recorded on the
    identity (it is reported), and a stub, if the identity needs one, is built from the
    lowest Linear id."""
    out = StubBuild()
    old = [e for e in entries if e.dataset == LINEAR_FOUND]
    stem_by_linear = {
        e.record.linear_customer_id: e.source_id for e in old if e.record.linear_customer_id
    }
    note_by_stem = {e.source_id: e.record.notes for e in old}

    groups: dict[str, list[LiveCustomer]] = {}
    for lv in sorted(managed, key=lambda c: c.id):
        stem = stem_by_linear.get(lv.id)
        keys = keys_for_live(lv)
        if stem is not None:
            keys = ItemKeys(
                label=keys.label,
                ids=(stem, *keys.ids),
                linear_id=keys.linear_id,
                domains=keys.domains,
                names=keys.names,
            )
        hit = resolve(keys, store.table)
        if hit is not None:
            cid = hit.canonical_id
        else:
            name = strip_status_suffix(lv.name)
            base = stem or slug_for(CustomerRecord(canonical_name=name, domains=lv.domains))
            # No Linear source yet: a second unresolved customer may land on this new
            # identity, and recording the first id would settle the pick by id order. The
            # primary's id is recorded below once the group is known.
            cid = store.add_identity(
                base or "customer", names=[name], domains=list(lv.domains), sources={}
            )
        groups.setdefault(cid, []).append(lv)

    with_record = set()
    for e in entries:
        if e.dataset != NOTION:
            continue
        hit = resolve(keys_for_record(e.record, dataset=NOTION, source_id=e.source_id), store.table)
        if hit is not None:
            with_record.add(hit.canonical_id)

    for cid, group in groups.items():
        primary = group[0] if len(group) == 1 else pick_primary(cid, group, store.table)
        if primary is None:
            # Ambiguous: record nothing on the identity, so the planner's error keeps
            # failing `check` until a human records one or merges the customers in Linear.
            conflict = stamped_conflict(cid, group)
            if conflict is not None:
                out.reports.append(f"linear-found: {conflict}; nothing recorded")
            else:
                named = ", ".join(f"'{lv.name}' ({lv.id})" for lv in group)
                out.reports.append(
                    f"linear-found: {cid}: no customer carries the canonical or recorded "
                    f"Linear id ({named}); nothing recorded: record one in identities.yaml "
                    "or merge in Linear"
                )
            primary = group[0]
        else:
            out.duplicates += [
                f"{cid}: '{lv.name}' ({lv.id}) is a known duplicate; merge it in Linear"
                for lv in group
                if lv is not primary
            ]
            store.add_values(
                cid, domains=list(primary.domains), sources={SOURCE_LINEAR: primary.id}
            )
        if cid in with_record:
            continue
        note = note_by_stem.get(cid) or STUB_NOTE.format(date=today.isoformat())
        out.stubs.append((cid, _stub(primary, cfg, note)))
    out.stubs.sort(key=lambda p: p[0])
    return out


def status_errors(entries: list[DatasetRecord], cfg: DatasetConfig) -> list[str]:
    """A stub whose Linear status the table does not map fails `check`."""
    errors: list[str] = []
    for e in entries:
        rec = e.record
        if e.dataset != LINEAR_FOUND:
            continue
        status = rec.status or ""
        if _classification(cfg, status) is not None:
            if rec.classification is None:
                errors.append(
                    f"stub '{rec.canonical_name}' has no classification: "
                    "run ingest --only linear-found"
                )
            continue
        lid = rec.linear_customer_id
        errors.append(
            f"unmapped Linear status: '{rec.canonical_name}' ({lid}) has status '{status}'. "
            f"Add '{status}: prospect' (or the right class) to linear_status_classification "
            f"in datasets.yaml, or ignore it: customer-registry-sync ignore "
            f'--linear-id {lid} --reason "<why>"'
        )
    return errors
