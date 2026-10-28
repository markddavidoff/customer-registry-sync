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

Live customers are split once into managed and ignored (`ignore.partition_live`);
drift, the plan, untracked values and stale retire entries see only the managed
set. A Linear edit to a stub (a `linear_owned` row) is not drift.

Drift detection and the exit codes come from `gitops_engine` (through
`linear_adapter.LinearCustomersAdapter`); the plan is the local field-level
planner, `deploy.plan_deploy`, the same one `deploy` runs.

`offline_errors` is the part of `check` that needs no Linear access: the
generated directories must match their manifests (no hand edits), the ignore
file must be valid, the identity table and datasets must resolve cleanly, and
`<data_dir>/linear/` and the two CSVs must equal what `generate` produces from
the datasets now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine import EXIT_DRIFT, EXIT_OK, EXIT_OPERROR, DriftReport
from gitops_engine import detect_drift as _engine_detect_drift
from gitops_engine.datasets import DATASETS_SUBDIR
from gitops_engine.manifest import manifest_edits

from .curated import CuratedFields
from .datasets import DatasetRecord, datasets_errors, load_datasets, load_datasets_config
from .deploy import DeployPlan, pair_rows, plan_deploy
from .fieldplan import Untracked, stale_retire, untracked_values
from .generate import canonical_id_for, generate_from_dir, group_by_identity
from .identity import (
    IDENTITIES_FILE,
    SOURCE_LINEAR,
    Identity,
    IdentityTable,
    conflict,
    keys_for_live,
    keys_for_record,
    load_identities,
    resolve,
    table_errors,
)
from .ignore import (
    IGNORE_FILE,
    IgnoreList,
    ignore_conflicts,
    ignore_errors,
    partition_live,
    stale_ignore,
)
from .linear_adapter import LinearCustomersAdapter
from .linear_client import LiveCustomer
from .linear_found import status_errors
from .linear_schema import LinearCustomer
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
    (`gitops_engine.detect_drift`). `linear_owned` are the live ids of stub rows:
    Linear owns them, so a content change there is not drift."""
    rep = _engine_detect_drift(state, live, LinearCustomersAdapter())
    if linear_owned:
        rep.changed = [(s, lv) for s, lv in rep.changed if lv.id not in linear_owned]
    return rep


@dataclass
class CheckResult:
    code: int
    drift: DriftReport
    plan: DeployPlan
    untracked: list[Untracked] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    # ignore conflicts, a live customer on two identities: fail
    failures: list[str] = field(default_factory=list)
    # stubs of ignored customers, stale ignore entries, unused identities,
    # records with no identity: report and pass
    reports: list[str] = field(default_factory=list)
    ignored: list[LiveCustomer] = field(default_factory=list)
    # managed Linear customers in no dataset: fail, each with `orphan_lines`
    orphans: list[LiveCustomer] = field(default_factory=list)


def _state_ignored(
    s: LinearCustomer, ignored_ids: set[str], ignore: IgnoreList, scope: str | None
) -> bool:
    if s.linear_synced is None:
        return False
    lid = s.linear_synced.linear_id
    return lid in ignored_ids or ignore.match(lid, s.name, scope=scope) is not None


def _with_record_identities(table: IdentityTable, entries: list[DatasetRecord]) -> IdentityTable:
    """A copy of `table` plus an implicit identity for each record that resolves to none.

    The id is `canonical_id_for` (a stub's file stem, else `slug_for`), with a numeric
    suffix when another identity already has it. Records go in order, so a later
    record of the same company resolves to the identity an earlier one created."""
    out = IdentityTable(entries=dict(table.entries))
    for e in entries:
        keys = keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id)
        if resolve(keys, out) is not None:
            continue
        base = canonical_id_for(e, out)
        cid, n = base, 1
        while cid in out.entries:
            n += 1
            cid = f"{base}-{n}"
        sources = (
            {SOURCE_LINEAR: e.record.linear_customer_id} if e.record.linear_customer_id else {}
        )
        out.entries[cid] = Identity(
            canonical_id=cid,
            values={"names": [e.record.canonical_name], "domains": list(e.record.domains)},
            sources=sources,
        )
    return out


def _record_identities(table: IdentityTable, entries: list[DatasetRecord]) -> set[str]:
    """Canonical ids of `table` that at least one record resolves to."""
    out: set[str] = set()
    for e in entries:
        hit = resolve(keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id), table)
        if hit is not None:
            out.add(hit.canonical_id)
    return out


def orphans(
    managed: list[LiveCustomer],
    table: IdentityTable,
    entries: list[DatasetRecord],
) -> list[LiveCustomer]:
    """Managed Linear customers in no dataset: they resolve to no identity, or to
    one that no record in any dataset resolves to. A record with no identity of
    its own counts through its implicit one."""
    full = _with_record_identities(table, entries)
    held = _record_identities(full, entries)
    out: list[LiveCustomer] = []
    for lv in managed:
        hit = resolve(keys_for_live(lv), full)
        if hit is None or hit.canonical_id not in held:
            out.append(lv)
    return out


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
    held_domains: dict[str, set[str]] = {}
    for keys, domains in (
        *(
            (keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id), e.record.domains)
            for e in entries
        ),
        *((keys_for_live(lv), lv.domains) for lv in live),
    ):
        hit = resolve(keys, table)
        if hit is not None:
            held_domains.setdefault(hit.canonical_id, set()).update(d.lower() for d in domains)
    out: list[str] = []
    for cid, ident in table.entries.items():
        if cid not in held_domains:
            out.append(f"unused identity: {cid}")
            continue
        out += [
            f"unused identity domain: {cid}: {d}"
            for d in ident.values["domains"]
            if d.lower() not in held_domains[cid]
        ]
    return out


def unidentified_records(table: IdentityTable, entries: list[DatasetRecord]) -> list[str]:
    """A record that resolves to no identity is reported; `ingest` gives it one."""
    return [
        f"no identity: {e.record.canonical_name}: run ingest"
        for e in entries
        if resolve(keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id), table)
        is None
    ]


def live_identity_conflicts(managed: list[LiveCustomer], table: IdentityTable) -> list[str]:
    """A Linear customer whose keys point at two identities fails `check`."""
    out: list[str] = []
    for lv in managed:
        msg = conflict(keys_for_live(lv), table)
        if msg is not None:
            out.append(
                f"{msg} (Linear id {lv.id}): merge the two identities in "
                f"{IDENTITIES_FILE}, or remove the value that points at the wrong one"
            )
    return out


def run_check(
    desired: list[LinearCustomer],
    state: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
    *,
    retire: dict[str, CuratedFields] | None = None,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
    entries: list[DatasetRecord] | None = None,
    other_live: list[LiveCustomer] | None = None,
) -> CheckResult:
    """Partition `live` once; every check below sees the managed set.

    `ignore=None` is the built-in prefix only. `entries` (the dataset records)
    let an ignored customer's own stub be reported, not failed; with them `check`
    also fails orphans and reports unused identities and records with no
    identity. `entries=None` means the records are not supplied: those checks are
    skipped.
    `other_live` are further live customers outside this run (a scoped run's
    out-of-scope rows) whose domains still count as held."""
    ignore = ignore if ignore is not None else IgnoreList()
    table = table or IdentityTable()
    managed, ignored = partition_live(live, ignore, scope=scope)
    ignored_ids = {lv.id for lv in ignored}
    state = [s for s in state if not _state_ignored(s, ignored_ids, ignore, scope)]
    stubs = {
        lv.id
        for d, lv in pair_rows(desired, managed, table).pairs
        if lv is not None and d.linear_owned
    }
    drift = detect_drift(state, managed, linear_owned=stubs)
    plan = plan_deploy(
        desired,
        managed,
        table,
        state=state,
        retire=retire,
        other_live=[*ignored, *(other_live or [])],
    )
    untracked = untracked_values(desired, managed, table, retire)
    stale = stale_retire(desired, managed, table, retire)
    failures, reports = ignore_conflicts(
        ignored, ignore, entries or [], desired, table, scope=scope
    )
    failures += live_identity_conflicts(managed, table)
    if scope is None:  # a scoped run sees only its own rows: every entry would look stale
        reports += stale_ignore(ignore, live)
    found: list[LiveCustomer] = []
    if entries is not None:
        found = orphans(managed, table, entries)
        reports += unidentified_records(table, entries)
        if scope is None:  # a scoped run sees only its own customers: most would look unused
            reports += unused_identities(table, entries, live)
    # untracked values are reported, not failed
    failing = (
        drift.any() or plan.aborts or plan.unacked or plan.errors or stale or failures or found
    )
    return CheckResult(
        EXIT_DRIFT if failing else EXIT_OK,
        drift,
        plan,
        untracked,
        stale,
        failures,
        reports,
        ignored,
        found,
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
    errors = table_errors(table)
    for entry in entries:
        keys = keys_for_record(entry.record, dataset=entry.dataset, source_id=entry.source_id)
        msg = conflict(keys, table)
        if msg is not None:
            errors.append(msg)
    for cid, group in group_by_identity(entries, table, []).items():
        per_dataset: dict[str, list[str]] = {}
        for entry in group:
            per_dataset.setdefault(entry.dataset, []).append(entry.source_id)
        for dataset, stems in per_dataset.items():
            if len(stems) > 1:
                files = ", ".join(f"{s}.yaml" for s in sorted(stems))
                errors.append(
                    f"two records in {DATASETS_SUBDIR}/{dataset} resolve to {cid}: {files}"
                )
    return errors


def _read_csv(path: Path) -> str | None:
    # bytes, not read_text: the CSV writer's \r\n line ends must compare exactly
    return path.read_bytes().decode("utf-8") if path.is_file() else None


def offline_errors(data_dir: Path) -> list[str]:
    """Hand-edited generated files, identity problems, stubs with an unmapped Linear
    status and a stale `generate` output (`linear/` and both CSVs); empty when clean."""
    root = Path(data_dir)
    errors: list[str] = datasets_errors(root)
    errors += ignore_errors(root / IGNORE_FILE)
    for sub, generator in generated_dirs(root).items():
        edits = manifest_edits(root / sub)
        if edits is None:
            errors.append(f"missing manifest: {sub}")
            continue
        errors += [
            f"hand-edited generated file: {sub}/{edit.split(': ', 1)[1]}: run {generator}"
            for edit in edits
        ]
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
