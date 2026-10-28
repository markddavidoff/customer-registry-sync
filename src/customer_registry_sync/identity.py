"""The customer identity declarations: which keys say "same customer" and how each compares.

`identities.yaml` (in `data_dir`) holds one entry per company, keyed by its permanent
canonical id (stamped into Linear as the external id). The table, ordered-key `resolve`
and the comment-preserving store are engine code (`gitops_engine.identity`); this module
declares the customer's keys: source id (`notion`, then `linear`), then domain, then
name. Domains compare case-insensitively; names compare through `norm_name`.

The functions below bind `IDENTITY_SPEC` to the engine's, so callers pass no spec.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gitops_engine import identity as engine
from gitops_engine import identity_store as engine_store
from gitops_engine.identity import (  # noqa: F401 (re-export)
    Hit,
    Identity,
    IdentityTable,
    ItemKeys,
    MatchKey,
    load_text,
)

from .datasets import LINEAR_FOUND, NOTION
from .linear_schema import LinearCustomer
from .schema import CustomerRecord

if TYPE_CHECKING:
    from .linear_client import LiveCustomer

SOURCE_NOTION = "notion"
SOURCE_LINEAR = "linear"
SOURCE_KEYS = (SOURCE_NOTION, SOURCE_LINEAR)

# Linear's customer search does not surface status, so every managed customer's
# display name carries it: "<name> [<status>]".
_STATUS_SUFFIX_RE = re.compile(r" \[(?:Active|Prospect|Churned)\]$")


def strip_status_suffix(name: str) -> str:
    """Remove a trailing ` [Active]` / ` [Prospect]` / ` [Churned]`, if present."""
    stripped = _STATUS_SUFFIX_RE.sub("", name)
    return stripped or name


def norm_name(name: str) -> str:
    """Status suffix stripped, then case, spaces and punctuation ignored."""
    return re.sub(r"[^a-z0-9]+", "", strip_status_suffix(name).lower())


IDENTITY_SPEC = engine.IdentitySpec(
    sources=SOURCE_KEYS,
    match_keys=(
        MatchKey("domains", "domain", str.lower),
        MatchKey("names", "name", norm_name),
    ),
    destination_source=SOURCE_LINEAR,
    file_order=("names", "domains", "sources"),
)
IDENTITIES_FILE = IDENTITY_SPEC.file

IDENTITIES_HEADER = (
    "# One entry per company. The key is the canonical id, stamped into Linear as the\n"
    "# external id; it never changes. Merge duplicates by editing this file.\n"
)


class IdentityStore(engine_store.IdentityStore):
    """The customer identity file, loaded for appending."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, IDENTITY_SPEC, header=IDENTITIES_HEADER)


def keys_for_record(rec: CustomerRecord, *, dataset: str, source_id: str) -> ItemKeys:
    """Keys for a dataset record; `source_id` is its file stem."""
    label = rec.canonical_name
    values = (("domains", tuple(rec.domains)), ("names", (rec.canonical_name,)))
    if dataset == NOTION:
        return ItemKeys(label=label, sources=((SOURCE_NOTION, source_id),), values=values)
    if dataset == LINEAR_FOUND:
        return ItemKeys(
            label=label,
            ids=(source_id,),
            sources=((SOURCE_LINEAR, rec.linear_customer_id),),
            values=values,
        )
    raise ValueError(f"unknown dataset {dataset!r}")


def keys_for_live(lv: LiveCustomer) -> ItemKeys:
    return ItemKeys(
        label=lv.name,
        ids=tuple(lv.external_ids),
        sources=((SOURCE_LINEAR, lv.id),),
        values=(("domains", tuple(lv.domains)), ("names", (lv.name,))),
    )


def keys_for_row(row: LinearCustomer) -> ItemKeys:
    return ItemKeys(
        label=row.name,
        ids=tuple(row.external_ids),
        sources=((SOURCE_LINEAR, row.linear_synced.linear_id if row.linear_synced else None),),
        values=(("domains", tuple(row.domains)), ("names", (row.name,))),
    )


def implicit_identity(row: LinearCustomer, taken: set[str]) -> Identity:
    """The identity for a row that resolves to nothing.

    The canonical id is the row's external id (generated rows carry `slug_for`), else a
    slug of its name, with a numeric suffix when `taken` already has it."""
    if row.external_ids:
        base = row.external_ids[0]
    else:
        base = re.sub(r"[^a-z0-9]+", "-", strip_status_suffix(row.name).lower()).strip("-")
        base = base or "customer"
    cid, n = base, 1
    while cid in taken:
        n += 1
        cid = f"{base}-{n}"
    sources = {SOURCE_LINEAR: row.linear_synced.linear_id} if row.linear_synced else {}
    return Identity(
        canonical_id=cid,
        values={"names": [strip_status_suffix(row.name)], "domains": list(row.domains)},
        sources=sources,
    )


# --- the engine's functions with the customer spec bound ---------------------------


def parse_identities(raw: Any, name: str) -> IdentityTable:
    return engine.parse_identities(raw, name, IDENTITY_SPEC)


def load_identities(path: Path) -> IdentityTable:
    """Load `identities.yaml`. A missing file is an empty table."""
    return engine.load_identities(path, IDENTITY_SPEC)


def resolve(item: ItemKeys, table: IdentityTable) -> Hit | None:
    """The first hit in key order decides: source id, then domain, then name."""
    return engine.resolve(item, table, IDENTITY_SPEC)


def resolve_all(item: ItemKeys, table: IdentityTable) -> list[Hit]:
    return engine.resolve_all(item, table, IDENTITY_SPEC)


def conflict(item: ItemKeys, table: IdentityTable) -> str | None:
    return engine.conflict(item, table, IDENTITY_SPEC)


def table_errors(table: IdentityTable) -> list[str]:
    """A domain, name or source id listed on two identities."""
    return engine.table_errors(table, IDENTITY_SPEC)


def with_implicit(table: IdentityTable, rows: list[LinearCustomer]) -> IdentityTable:
    """A copy of `table` plus one identity per row that resolves to nothing."""
    return engine.with_implicit(
        table, rows, IDENTITY_SPEC, keys=keys_for_row, implicit=implicit_identity
    )
