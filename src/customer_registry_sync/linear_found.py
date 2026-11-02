"""The `linear-found` dataset: a stub for each Linear customer the Notion dataset lacks.

`ingest` builds it offline from the state mirror. Each managed (non-hidden) Linear
customer is resolved against the identity table. One that resolves to nothing gets a
new identity first; one whose identity has no `notion` record becomes a stub. Every
primary customer's Linear id is appended to its identity, and a stub's Linear-only
domains too. A customer with a `notion` record learns no domain from Linear: a domain
added in Linear reaches the registry only through `linear-overrides.yaml`.
A stub's file stem is its canonical id, and a stub keeps that stem across runs, keyed
by its Linear id, so the external id stamped into Linear never drifts when the
customer's name or domain changes there.

When a Notion record starts to resolve to a stub's identity, the Notion row wins and
the stub goes. `handover_entries` gives the overrides that keep the revenue, tier and
domains the stub carried in Linear but the Notion row lacks, so the handover clears
nothing in Linear (the same comparison as `adopt-live`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from gitops_engine.found import FoundBuild, build_found
from gitops_engine.hidden import hidden_from
from gitops_engine.overrides import OverrideEntry, adopt_entry

from .datasets import LINEAR_FOUND, NOTION, DatasetConfig, DatasetRecord, load_datasets_config
from .generate import canonical_id_for, merge_datasets
from .identity import (
    IDENTITY_SPEC,
    SOURCE_NOTION,
    IdentityStore,
    IdentityTable,
    keys_for_record,
    resolve,
    strip_status_suffix,
)
from .linear_client import LiveCustomer
from .linear_overrides import load_overrides_file, uncurated
from .linear_schema import LINEAR_SCHEMA
from .schema import VALID_CLASSIFICATIONS, CustomerRecord
from .writers import load_state, slug_for

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

    `managed` must already exclude hidden customers. Customers are taken in Linear-id
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
        learn_with_record=False,
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
            f"in datasets.yaml, or hide it: customer-registry-sync hide "
            f'--linear-id {lid} --reason "<why>"'
        )
    return errors


def stub_ids(entries: list[DatasetRecord], table: IdentityTable) -> set[str]:
    """The canonical ids of the `linear-found` stubs among `entries`."""
    return {canonical_id_for(e, table) for e in entries if e.dataset == LINEAR_FOUND}


def handover_entries(
    data_dir: Path,
    old_stub_ids: set[str],
    new_stub_ids: set[str],
    entries: list[DatasetRecord],
    table: IdentityTable,
) -> dict[str, OverrideEntry]:
    """Canonical id -> the overrides that keep a handed-over stub's Linear values.

    A stub hands over when its id is in `old_stub_ids` but not `new_stub_ids` and a
    `notion` record in `entries` now resolves to it. A hidden or deleted customer hands
    nothing over. For each, the upstream desired row (`merge_datasets` without
    overrides, the old stub left out) is compared through `adopt_entry` with the
    mirror row for the old stub's Linear id: the result pins each revenue or tier the
    Notion row leaves empty and adds each domain it lacks. An id with nothing to keep,
    or no mirror row, is absent."""
    with_record = {canonical_id_for(e, table) for e in entries if e.dataset == NOTION}
    handed = (old_stub_ids - new_stub_ids) & with_record
    if not handed:
        return {}
    overrides = load_overrides_file(data_dir)
    hidden = hidden_from(overrides)
    linear_ids: dict[str, str] = {}
    upstream_entries: list[DatasetRecord] = []
    for e in entries:
        cid = canonical_id_for(e, table)
        if e.dataset == LINEAR_FOUND and cid in handed:
            if e.record.linear_customer_id:
                linear_ids[cid] = e.record.linear_customer_id
            continue
        upstream_entries.append(e)
    mirror = {
        s.linear_synced.linear_id: LINEAR_SCHEMA.state_to_live(s)
        for s in load_state(data_dir)
        if s.linear_synced is not None
    }
    rows = merge_datasets(upstream_entries, table, load_datasets_config(data_dir).names).upstream
    out: dict[str, OverrideEntry] = {}
    for row in rows:
        cid = row.external_ids[0] if row.external_ids else ""
        lv = mirror.get(linear_ids.get(cid, ""))
        if cid not in handed or lv is None:
            continue
        current = overrides.entries.get(cid)
        if current is not None and current.deleted is not None:
            continue
        if hidden.match(cid, LINEAR_SCHEMA.live_label(lv), scope=None) is not None:
            continue
        entry = adopt_entry(row, lv, schema=LINEAR_SCHEMA)
        if entry is not None and current is not None:
            entry = uncurated(entry, current)
        if entry is not None:
            out[cid] = entry
    return out
