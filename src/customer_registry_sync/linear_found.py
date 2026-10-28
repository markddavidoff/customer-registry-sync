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

from datetime import date

from gitops_engine.found import FoundBuild, build_found

from .datasets import LINEAR_FOUND, NOTION, DatasetConfig, DatasetRecord
from .identity import (
    IDENTITY_SPEC,
    SOURCE_NOTION,
    IdentityStore,
    keys_for_record,
    resolve,
    strip_status_suffix,
)
from .linear_client import LiveCustomer
from .linear_schema import LINEAR_SCHEMA
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
) -> FoundBuild[CustomerRecord]:
    """Stubs for the managed customers whose identity has no `notion` record.

    `managed` must already exclude ignored customers. Customers are taken in Linear-id
    order. Of several on one identity, the primary is the one carrying the canonical id
    or the recorded Linear id (as the planner picks it); the others are known duplicates
    and contribute nothing. When neither id is on any of them, nothing is recorded on the
    identity (it is reported), and a stub, if the identity needs one, is built from the
    lowest Linear id. The walk itself is `gitops_engine.found.build_found`."""

    def new_id(lv: LiveCustomer, stem: str | None) -> str:
        name = strip_status_suffix(lv.name)
        return (
            stem or slug_for(CustomerRecord(canonical_name=name, domains=lv.domains)) or "customer"
        )

    def stub(cid: str, lv: LiveCustomer, previous: CustomerRecord | None) -> CustomerRecord:
        note = (previous.notes if previous else None) or STUB_NOTE.format(date=today.isoformat())
        return _stub(lv, cfg, note)

    return build_found(
        managed,
        store,
        entries,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        found_dataset=LINEAR_FOUND,
        primary_dataset=NOTION,
        record_keys=lambda e: keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id),
        new_id=new_id,
        stub=stub,
        new_values=lambda lv: {
            "names": [strip_status_suffix(lv.name)],
            "domains": list(lv.domains),
        },
        learned_values=lambda lv: {"domains": list(lv.domains)},
    )


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
