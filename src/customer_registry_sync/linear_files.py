"""The Linear sync's hand-curated files, declared on the engine's loaders.

`linear-ignore.yaml` and `linear-retire.yaml` are read and written by `gitops_engine`
(`ignore`, `retire`); this module names them for Linear customers, binds the engine's
callbacks for `ignore_conflicts`, and adapts a retire entry to `CuratedFields` for the
planner until the planner reads retire entries itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine.datasets import DATASETS_FILE, DatasetRecord
from gitops_engine.ignore import IgnoreFormat, IgnoreList
from gitops_engine.ignore import ignore_conflicts as engine_ignore_conflicts
from gitops_engine.retire import RetireEntry, load_retire

from .datasets import LINEAR_FOUND
from .generate import canonical_id_for
from .identity import (
    IDENTITIES_FILE,
    IDENTITY_SPEC,
    IdentityTable,
    implicit_identity,
    keys_for_record,
    strip_status_suffix,
)
from .linear_client import LiveCustomer
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .schema import CustomerRecord

IGNORE_FILE = "linear-ignore.yaml"
IGNORE_HEADER = "# Linear customers the sync never touches. Each entry needs a reason.\n"
IGNORE_FORMAT = IgnoreFormat(IGNORE_FILE, "linear_id", IGNORE_HEADER, "customers")
RETIRE_FILE = "linear-retire.yaml"

# The curated files `generate`, `check` and `deploy` read directly, as names relative
# to the registry's `data_dir`. A change to any of them changes what `deploy` may
# write, so each triggers the deploy workflow.
CURATED_INPUTS: tuple[str, ...] = (IDENTITIES_FILE, IGNORE_FILE, DATASETS_FILE, RETIRE_FILE)


def ignore_conflicts(
    ignored: list[LiveCustomer],
    ignore: IgnoreList,
    entries: list[DatasetRecord[CustomerRecord]],
    desired: list[LinearCustomer],
    table: IdentityTable,
    *,
    scope: str | None,
) -> tuple[list[str], list[str]]:
    """The engine's `ignore_conflicts` with the customer callbacks bound."""
    return engine_ignore_conflicts(
        ignored,
        ignore,
        entries,
        desired,
        table,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        found_dataset=LINEAR_FOUND,
        record_keys=lambda e: keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id),
        record_live_id=lambda rec: rec.linear_customer_id,
        fallback_id=lambda e: canonical_id_for(e, table),
        implicit=implicit_identity,
        describe=lambda d: strip_status_suffix(d.name),
        scope=scope,
    )


@dataclass
class CuratedFields:
    """A retire entry as the planner reads it. Task 6 deletes this with the planner's
    per-field code; until then `load_retire_fields` converts to it."""

    domains: list[str] = field(default_factory=list)
    revenue: int | None = None
    tier: str | None = None

    def empty(self) -> bool:
        return not self.domains and self.revenue is None and self.tier is None


def retire_fields(entries: dict[str, RetireEntry]) -> dict[str, CuratedFields]:
    """Each engine `RetireEntry` as the `CuratedFields` the planner takes."""
    return {
        ext_id: CuratedFields(
            domains=list(entry.get("domains") or []),
            revenue=entry.get("revenue"),
            tier=entry.get("tier"),
        )
        for ext_id, entry in entries.items()
    }


def load_retire_fields(path: Path) -> dict[str, CuratedFields]:
    """`linear-retire.yaml` as `CuratedFields` (a missing or empty file yields `{}`)."""
    return retire_fields(load_retire(path, LINEAR_SCHEMA))
