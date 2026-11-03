"""The Linear sync's hand-curated files, declared on the engine's loaders.

`linear-overrides.yaml` is named in `linear_overrides`; this module binds the customer
schema, identity spec and dataset callbacks to the engine's hidden-row functions
(`gitops_engine.hidden`), which read its `hidden` entries and `hidden_name_prefixes`.
`linear-ignore.yaml` and `linear-retire.yaml` are no longer read: `LEGACY_FILES` names
the files a data repo must migrate away from (`adopt-live`), which still reads the
ignore file through `IGNORE_FORMAT`.
"""

from __future__ import annotations

from collections.abc import Callable

from gitops_engine import hidden as engine_hidden
from gitops_engine.datasets import DATASETS_FILE, DatasetRecord
from gitops_engine.hidden import Hidden, hidden_from
from gitops_engine.ignore import IgnoreFormat
from gitops_engine.overrides import Overrides

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
from .linear_overrides import OVERRIDES_FILE
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .schema import CustomerRecord

IGNORE_FILE = "linear-ignore.yaml"
IGNORE_HEADER = "# Linear customers the sync never touches. Each entry needs a reason.\n"
IGNORE_FORMAT = IgnoreFormat(IGNORE_FILE, "linear_id", IGNORE_HEADER, "customers")
RETIRE_FILE = "linear-retire.yaml"
# Pre-apply snapshots, one file per `deploy --apply`: `<data_dir>/linear-snapshots/`.
SNAPSHOT_SUBDIR = "linear-snapshots"

# The curated files `generate`, `check` and `deploy` read directly, as names relative
# to the registry's `data_dir`. A change to any of them changes what `deploy` may
# write, so each triggers the deploy workflow.
CURATED_INPUTS: tuple[str, ...] = (IDENTITIES_FILE, OVERRIDES_FILE, DATASETS_FILE)

# Files an earlier version read, which `linear-overrides.yaml` replaces. `check` and
# `deploy`, `ingest`, `sync-inbound` and `refresh` refuse to run while one exists
# (`deploy_plan.legacy_refused`).
LEGACY_FILES: tuple[str, ...] = (IGNORE_FILE, RETIRE_FILE)


def no_hidden() -> Hidden:
    """No hidden entries: the built-in prefix only (a missing overrides file)."""
    return hidden_from(Overrides(OVERRIDES_FILE))


def partition_hidden(
    live: list[LiveCustomer], hidden: Hidden, table: IdentityTable, *, scope: str | None
) -> tuple[list[LiveCustomer], list[LiveCustomer]]:
    """(managed, hidden) live customers (`gitops_engine.hidden.partition_hidden`). A
    customer is hidden by id only when its keys resolve, through `table`, to a canonical
    id with a `hidden` entry."""
    return engine_hidden.partition_hidden(
        live, hidden, table, schema=LINEAR_SCHEMA, spec=IDENTITY_SPEC, scope=scope
    )


def hidden_reason(
    hidden: Hidden, table: IdentityTable, *, scope: str | None
) -> Callable[[LiveCustomer], str | None]:
    """Why a live customer is hidden (or None), resolved through `table`: the `exclude`
    hook `deploy.apply_deploy` runs on each row it re-reads at write time."""
    return lambda lv: engine_hidden.live_reason(
        lv, hidden, table, schema=LINEAR_SCHEMA, spec=IDENTITY_SPEC, scope=scope
    )


def hidden_lines(
    hidden_live: list[LiveCustomer], hidden: Hidden, table: IdentityTable, *, scope: str | None
) -> list[str]:
    """`check`'s HIDDEN lines (`gitops_engine.hidden.hidden_lines`)."""
    return engine_hidden.hidden_lines(
        hidden_live, hidden, table, schema=LINEAR_SCHEMA, spec=IDENTITY_SPEC, scope=scope
    )


def hidden_conflicts(
    hidden_live: list[LiveCustomer],
    hidden: Hidden,
    entries: list[DatasetRecord[CustomerRecord]],
    desired: list[LinearCustomer],
    table: IdentityTable,
    *,
    scope: str | None,
) -> tuple[list[str], list[str]]:
    """The engine's `hidden_conflicts` with the customer callbacks bound."""
    return engine_hidden.hidden_conflicts(
        hidden_live,
        hidden,
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
