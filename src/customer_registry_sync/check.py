"""`check` — the plan guard a CI job keys off (exit 3 fails it).

Drift = the repo's recorded picture of Linear (`<data_dir>/linear-state/`) no
longer matches live, judged by `content_hash` of the deployable fields (NOT
`updatedAt`, which Linear bumps on edits to fields we ignore). Three classes:
changed (state≠live), added (live row with no state row), deleted (state row
with no live row). `check` also fails on unacknowledged destructive changes,
retire entries that no longer match (stale), planner errors (two desired rows on
one identity, duplicates with no canonical or recorded Linear id), ignore
conflicts, a Linear customer in no dataset (an orphan) and a live customer
resolving to two identities. Exit 3 on any of these or a plan-time abort; 0 when
in sync.

Untracked values (a live domain, revenue or tier the registry lacks), known
duplicates, withheld domains, unused identities, records with no identity,
stale ignore entries and the ignored-customer list are reported, never failed:
the next refresh appends an untracked domain to its identity, and the Notion
dataset is the only place to maintain revenue and tier.

Live customers are split once into managed and ignored (`gitops_engine.ignore.partition_live`);
drift, the plan, untracked values and stale retire entries see only the managed
set. A Linear edit to a stub (a `linear_owned` row) is not drift.

The checks themselves are the engine's (`gitops_engine.check.run_check`, with drift
by `LINEAR_SCHEMA` and the plan from `deploy.plan_deploy`, the same one `deploy`
runs). This module binds the customer schema, identity spec and dataset records, and
keeps the orphan failure lines, which name this tool's commands.

`offline_errors` is the part of `check` that needs no Linear access: the
generated directories must match their manifests (no hand edits), the ignore
file must be valid, the identity table and datasets must resolve cleanly, and
`<data_dir>/linear/` and the two CSVs must equal what `generate` produces from
the datasets now.
"""

from __future__ import annotations

from pathlib import Path

from gitops_engine import EXIT_DRIFT, EXIT_OK, EXIT_OPERROR, DriftReport
from gitops_engine import check as engine
from gitops_engine.check import CheckResult, DatasetHooks
from gitops_engine.drift import detect_drift as engine_detect_drift
from gitops_engine.datasets import DATASETS_SUBDIR
from gitops_engine.ignore import IgnoreList, ignore_errors
from gitops_engine.retire import RetireEntry

from .datasets import (
    LINEAR_FOUND,
    DatasetRecord,
    datasets_errors,
    load_datasets,
    load_datasets_config,
)
from .generate import canonical_id_for, generate_from_dir
from .identity import (
    IDENTITIES_FILE,
    IDENTITY_SPEC,
    SOURCE_LINEAR,
    Identity,
    IdentityTable,
    implicit_identity,
    keys_for_record,
    load_identities,
)
from .linear_client import LiveCustomer
from .linear_files import IGNORE_FILE, IGNORE_FORMAT
from .linear_found import status_errors
from .identity import strip_status_suffix
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .schema import CustomerRecord
from .writers import (
    DESIRED_SUBDIR,
    REAL_CUSTOMERS_CSV,
    REGISTRY_CSV,
    STATE_SUBDIR,
    csv_text,
    load_desired,
    real_entries,
)

__all__ = [
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXIT_OPERROR",
    "CheckResult",
    "DriftReport",
    "detect_drift",
    "offline_errors",
    "orphan_lines",
    "run_check",
]


def detect_drift(
    state: list[LinearCustomer],
    live: list[LiveCustomer],
    *,
    linear_owned: set[str] | frozenset[str] = frozenset(),
) -> DriftReport:
    """Whether the recorded state mirror still matches live, by content hash
    (`gitops_engine.drift.detect_drift`). `linear_owned` are the live ids of stub rows:
    Linear owns them, so a content change there is not drift."""
    return engine_detect_drift(state, live, schema=LINEAR_SCHEMA, owned_ids=linear_owned)


def _record_keys(e: DatasetRecord[CustomerRecord]):
    return keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id)


def _record_identity(e: DatasetRecord[CustomerRecord], taken: set[str]) -> Identity:
    """The implicit identity of a record that resolves to none: `canonical_id_for` (a
    stub's file stem, else `slug_for`), with a numeric suffix when another identity
    already has it. Records go in order, so a later record of the same company
    resolves to the identity an earlier one created."""
    base = canonical_id_for(e, IdentityTable())
    cid, n = base, 1
    while cid in taken:
        n += 1
        cid = f"{base}-{n}"
    sources = {SOURCE_LINEAR: e.record.linear_customer_id} if e.record.linear_customer_id else {}
    return Identity(
        canonical_id=cid,
        values={"names": [e.record.canonical_name], "domains": list(e.record.domains)},
        sources=sources,
    )


def dataset_hooks(table: IdentityTable) -> DatasetHooks[CustomerRecord]:
    """How the engine's check reads the customer dataset records."""
    return DatasetHooks(
        keys=_record_keys,
        record_id=lambda e: canonical_id_for(e, table),
        implicit=_record_identity,
        live_id=lambda rec: rec.linear_customer_id,
        found_dataset=LINEAR_FOUND,
        held_key="domains",
        unidentified_hint="run ingest",
        describe=lambda d: strip_status_suffix(d.name),
    )


def orphans(
    managed: list[LiveCustomer],
    table: IdentityTable,
    entries: list[DatasetRecord],
) -> list[LiveCustomer]:
    """Managed Linear customers in no dataset: they resolve to no identity, or to
    one that no record in any dataset resolves to. A record with no identity of
    its own counts through its implicit one."""
    return engine.orphans(
        managed,
        table,
        entries,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        hooks=dataset_hooks(table),
    )


def orphan_lines(lv: LiveCustomer) -> list[str]:
    """The failure for one orphan: the cause, then both ways out."""
    return [
        f"orphan: Linear customer '{lv.name}' ({lv.id}) is in no dataset.",
        (
            "  Add it: dispatch the registry-sync workflow with mode=refresh (or run "
            "sync-inbound, ingest --only linear-found, generate) and merge the PR."
        ),
        f'  Or ignore it: customer-registry-sync ignore --linear-id {lv.id} --reason "<why>"',
    ]


def unused_identities(
    table: IdentityTable,
    entries: list[DatasetRecord],
    live: list[LiveCustomer],
) -> list[str]:
    """Report lines for identities no record and no Linear customer resolves to, and
    for domains of a used identity that none of them still holds."""
    return engine.unused_identities(
        table,
        entries,
        live,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        hooks=dataset_hooks(table),
    )


def run_check(
    desired: list[LinearCustomer],
    state: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
    *,
    retire: dict[str, RetireEntry] | None = None,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
    entries: list[DatasetRecord] | None = None,
    other_live: list[LiveCustomer] | None = None,
) -> CheckResult:
    """The engine's `run_check` with the customer schema, identity spec and dataset
    records bound. `ignore=None` is the built-in prefix only; `entries=None` skips the
    orphan, unused-identity and no-identity checks. `other_live` are further live
    customers outside this run (a scoped run's out-of-scope rows) whose domains still
    count as held."""
    table = table or IdentityTable()
    return engine.run_check(
        desired,
        state,
        live,
        table,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        implicit=implicit_identity,
        retire=retire,
        ignore=ignore if ignore is not None else IgnoreList(IGNORE_FILE),
        scope=scope,
        entries=entries,
        hooks=dataset_hooks(table),
        other_live=other_live,
    )


def generated_dirs(data_dir: Path) -> dict[str, str]:
    """Generated directory (relative to the data dir) -> the command that writes it."""
    cfg = load_datasets_config(data_dir)
    dirs = {f"{DATASETS_SUBDIR}/{name}": "ingest" for name in cfg.names}
    dirs[DESIRED_SUBDIR] = "generate"
    dirs[STATE_SUBDIR] = "sync-inbound"
    return dirs


def _row_key(rec: LinearCustomer) -> str:
    return rec.external_ids[0] if rec.external_ids else rec.name


def _stable_dict(rec: LinearCustomer) -> dict:
    d = rec.to_dict()
    d.pop("linear_synced", None)  # desired rows never carry it
    return d


def identity_offline_errors(entries: list[DatasetRecord], table: IdentityTable) -> list[str]:
    """Identity problems that change generated output; empty when clean."""
    return engine.identity_offline_errors(
        entries,
        table,
        spec=IDENTITY_SPEC,
        record_keys=_record_keys,
        record_id=lambda e: canonical_id_for(e, table),
    )


def _read_csv(path: Path) -> str | None:
    # bytes, not read_text: the CSV writer's \r\n line ends must compare exactly
    return path.read_bytes().decode("utf-8") if path.is_file() else None


def offline_errors(data_dir: Path) -> list[str]:
    """Hand-edited generated files, identity problems, stubs with an unmapped Linear
    status and a stale `generate` output (`linear/` and both CSVs); empty when clean."""
    root = Path(data_dir)
    errors: list[str] = datasets_errors(root)
    errors += ignore_errors(root / IGNORE_FILE, IGNORE_FORMAT)
    errors += engine.manifest_errors(root, generated_dirs(root))
    table = load_identities(root / IDENTITIES_FILE)
    cfg = load_datasets_config(root)
    entries = load_datasets(root, cfg)
    errors += identity_offline_errors(entries, table)
    errors += status_errors(entries, cfg)
    generated = generate_from_dir(root)
    expected = {_row_key(r): _stable_dict(r) for r in generated.rows}
    actual = {_row_key(r): _stable_dict(r) for r in load_desired(root)}
    errors += [
        f"{DESIRED_SUBDIR} out of date: {xid}: run generate"
        for xid in sorted(expected.keys() | actual.keys())
        if expected.get(xid) != actual.get(xid)
    ]
    for name, csv_entries in (
        (REGISTRY_CSV, generated.winners),
        (REAL_CUSTOMERS_CSV, real_entries(generated.winners)),
    ):
        if _read_csv(root / name) != csv_text(csv_entries):
            errors.append(f"{name} out of date: run generate")
    return errors
