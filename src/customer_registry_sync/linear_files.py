"""The Linear sync's hand-curated files, declared on the engine's loaders.

`linear-overrides.yaml` is named in `linear_overrides`. `linear-ignore.yaml` is read and
written by `gitops_engine.ignore`; this module names it for Linear customers and binds
the engine's callbacks for `ignore_conflicts`. `linear-retire.yaml` is no longer read:
`LEGACY_FILES` names the files a data repo must migrate away from (`adopt-live`).
"""

from __future__ import annotations

from gitops_engine.datasets import DATASETS_FILE, DatasetRecord
from gitops_engine.ignore import IgnoreFormat, IgnoreList
from gitops_engine.ignore import ignore_conflicts as engine_ignore_conflicts

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
# `deploy` refuse to run while a guarded one exists (`deploy_plan.LEGACY_GUARDED`).
LEGACY_FILES: tuple[str, ...] = (IGNORE_FILE, RETIRE_FILE)


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
