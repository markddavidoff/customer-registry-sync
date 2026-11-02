"""CLI for the customer registry.

Global option ``--config <path>`` names the YAML config file (default
``registry-sync.yaml`` in the working directory); see ``config.py``. Every
command's ``--out-dir`` defaults to the config's ``data_dir``.

Phase A:  ``ingest``        — Notion hub pages → the ``notion`` dataset, then the
                              ``linear-found`` stubs from the state mirror
                              (``--only linear-found``: offline); appends identities.
Phase B:  ``generate``      — datasets → Layer-2 ``<data_dir>/linear/*`` and the CSVs.
          ``sync-inbound``  — live Linear → Layer-2 mirror (refresh linear_synced);
                              Linear edits → ``linear-overrides.yaml``.
          ``deploy``        — Layer-2 → Linear, per-customer check-and-set.
                              DRY-RUN by default; ``--apply`` performs live writes
                              after snapshotting the rows it touches.
          ``restore``       — write a pre-apply snapshot back to Linear.
          ``adopt-live``    — migrate a v0.2.0 data dir, offline: keep Linear-only
                              values as overrides, turn the ignore file into hidden.
Phase C:  ``check``         — plan guard: drift (state vs live) + plan; exit 0/2/3.
Workflow: ``changed --since SHA``, ``config-get KEY``, ``refresh --no-input`` (sync-inbound,
          ``ingest --only linear-found``, ``generate``) and ``tool-source`` complete the CLI
          contract in gitops-engine ``docs/cli-contract.md``.

Layer 1 is the datasets (``<data_dir>/datasets/<name>/``); Layer 2 is the Linear projection:
desired customers (``linear/``) and the state mirror of live Linear (``linear-state/``).

``--prod`` is required to write Linear from the configured ``data_dir``, and a
``--prod`` write outside GitHub Actions also needs ``--break-glass``.

Secrets come from the environment: ``NOTION_API_TOKEN`` (ingest), ``LINEAR_API_TOKEN`` (Phase B).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from gitops_engine.changed import changed_since
from gitops_engine.datasets import DATASETS_SUBDIR, dataset_dir
from gitops_engine.destination import DestinationError
from gitops_engine.fieldplan import DESTRUCTIVE, FieldChange
from gitops_engine.hidden import hidden_from
from gitops_engine.manifest import MANIFEST, guard_overwrite, manifest_edits, write_manifest
from gitops_engine.overrides import PENDING_ADD, OverridesStore, override_errors

from .changed import deploy_inputs
from .check import (
    EXIT_DRIFT,
    EXIT_OPERROR,
    offline_errors,
    orphan_lines,
    override_findings,
    pending_lines,
)
from .config import DEFAULT_CONFIG_NAME, ConfigError, config_value, load_config
from .datasets import LINEAR_FOUND, NOTION, load_datasets, load_datasets_config
from .deploy import apply_deploy, restore
from .deploy_plan import (
    check_exit,
    check_for,
    legacy_refused,
    load_inputs,
    load_overrides_checked,
    plan_for_deploy,
    scope_recs,
)
from .generate import generate_from_dir
from .identity import IDENTITIES_FILE, IdentityStore
from .inbound import read_inbound, write_inbound
from .linear_client import LinearError
from .linear_destination import LinearDestination
from .linear_files import (
    SNAPSHOT_SUBDIR,
    hidden_lines,
    hidden_reason,
    partition_hidden,
)
from .linear_found import (
    append_record_identities,
    build_linear_found,
    handover_entries,
    stub_ids,
)
from .linear_overrides import (
    OVERRIDES_FILE,
    OVERRIDES_FORMAT,
    load_overrides_file,
    value_type_failures,
)
from .linear_schema import LINEAR_SCHEMA
from .migrate import AdoptRefused, adopt_live
from .sources import SourceError
from .tool_source import ToolSourceError, tool_source
from .writers import (
    DESIRED_SUBDIR,
    REAL_CLASSIFICATIONS,
    REAL_CUSTOMERS_CSV,
    REGISTRY_CSV,
    STATE_SUBDIR,
    load_state,
    write_csvs,
    write_dataset,
    write_desired,
    write_state,
)


def _out_dir(args) -> Path:
    """``--out-dir`` if given, else the config's ``data_dir``."""
    return args.out_dir if args.out_dir is not None else args.registry.data_dir


def _is_prod(args, out_dir: Path) -> bool:
    """The target is the configured ``data_dir``: the production data."""
    return out_dir.resolve() == args.registry.data_dir.resolve()


def _dataset_summary(records) -> str:
    by_class = Counter(r.classification for r in records)
    real = sum(1 for r in records if r.classification in REAL_CLASSIFICATIONS)
    return (
        f"records: {len(records)} (real+partner: {real})\n"
        f"by classification: {dict(by_class)}\n"
        f"with domain: {sum(1 for r in records if r.domains)}"
        f" | arr: {sum(1 for r in records if r.arr is not None)}"
        f" | deployment: {sum(1 for r in records if r.deployment)}"
    )


def _guard_or_refuse(directory: Path, generator: str, args) -> bool:
    """Ask before replacing hand-edited generated files; False means nothing was written."""
    if guard_overwrite(directory, generator, no_input=args.no_input):
        return True
    print(
        f"REFUSED — {directory} has hand edits; nothing written. Re-run `{generator}` "
        "on a terminal to confirm, or discard the edits.",
        file=sys.stderr,
    )
    return False


def _ingest_source(args, out_dir: Path) -> int:
    """The configured source → `datasets/notion/`. 0 when written (or a dry run), else 2."""
    try:
        records = args.registry.source.ingest(out_dir)
    except SourceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(_dataset_summary(records))
    target = dataset_dir(out_dir, NOTION)
    if args.dry_run:
        print(f"DRY RUN — would write {len(records)} dataset files under {target}/.")
        return 0
    if not _guard_or_refuse(target, "ingest", args):
        return 2
    write_dataset(records, out_dir, dataset=NOTION)
    write_manifest(target, "ingest")
    print(f"wrote {target}/*.yaml")
    return 0


def _handover(args, out_dir: Path, store: IdentityStore, old: set[str], new: set[str], entries):
    """Keep the Linear values of each stub handing over to Notion as accepted pins and
    domain adds in `linear-overrides.yaml` (`linear_found.handover_entries`)."""
    kept = handover_entries(out_dir, old, new, entries, store.table)
    if not kept:
        return
    if args.dry_run:
        print(f"DRY RUN — handover: would keep the Linear values of {len(kept)} customers")
        return
    overrides = OverridesStore(out_dir / OVERRIDES_FILE, OVERRIDES_FORMAT)
    for cid, entry in kept.items():
        overrides.adopt(cid, entry)
    overrides.save()
    print(f"handover: {len(kept)} customers kept their Linear values")


def _ingest_linear_found(args, out_dir: Path, store: IdentityStore, hidden, entries) -> int:
    """State mirror → `datasets/linear-found/`; 2 when refused. Hidden customers get no
    stub (and so no identity from one). A stub that a `notion` record now takes over
    keeps its Linear values (`_handover`)."""
    old_stubs = stub_ids(entries, store.table)
    managed, _ = partition_hidden(
        [
            LINEAR_SCHEMA.state_to_live(r)
            for r in load_state(out_dir)
            if r.linear_synced is not None
        ],
        hidden,
        store.table,
        scope=None,
    )
    build = build_linear_found(
        managed, store, entries, load_datasets_config(out_dir), today=datetime.now(UTC).date()
    )
    for msg in build.duplicates:
        print(f"  KNOWN DUPLICATE: {msg}")
    for msg in build.reports:
        print(f"  NOTE: {msg}")
    print(f"linear-found: {len(build.stubs)} stubs; known duplicates: {len(build.duplicates)}")
    target = dataset_dir(out_dir, LINEAR_FOUND)
    new_stubs = {cid for cid, _ in build.stubs}
    if args.dry_run:
        print(f"DRY RUN — would write {len(build.stubs)} stub files under {target}/.")
        _handover(args, out_dir, store, old_stubs, new_stubs, entries)
        return 0
    if not _guard_or_refuse(target, "ingest", args):
        return 2
    write_dataset(
        [rec for _, rec in build.stubs],
        out_dir,
        dataset=LINEAR_FOUND,
        stems=[cid for cid, _ in build.stubs],
    )
    write_manifest(target, "ingest")
    print(f"wrote {target}/*.yaml")
    _handover(args, out_dir, store, old_stubs, new_stubs, entries)
    return 0


def _cmd_ingest(args) -> int:
    """Build `notion` (Notion) and then `linear-found` (state mirror, offline),
    appending new identities and missing values to identities.yaml. In every mode, a
    stub a `notion` record now resolves to keeps its Linear values (`_handover`)."""
    out_dir = _out_dir(args)
    if legacy_refused(out_dir):
        return 2
    try:
        store = IdentityStore(out_dir / IDENTITIES_FILE)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    overrides = load_overrides_checked(out_dir)
    if overrides is None:
        return 2
    hidden = hidden_from(overrides)
    has_mirror = (out_dir / STATE_SUBDIR / MANIFEST).is_file()
    no_mirror = f"no state mirror at {out_dir / STATE_SUBDIR}: run sync-inbound first"
    if args.only == LINEAR_FOUND and not has_mirror:
        print(f"ERROR: {no_mirror}; nothing written.", file=sys.stderr)
        return 2
    if args.only != LINEAR_FOUND:
        code = _ingest_source(args, out_dir)
        if code:
            return code
    # Every dataset on disk, listed in datasets.yaml or not: existing stubs keep their ids.
    cfg = load_datasets_config(out_dir)
    entries = load_datasets(out_dir, replace(cfg, names=[NOTION, LINEAR_FOUND]))
    before = len(store.table.entries)
    append_record_identities(store, entries)
    if args.only != NOTION:
        if has_mirror:
            code = _ingest_linear_found(args, out_dir, store, hidden, entries)
            if code:
                return code
        else:
            print(f"linear-found: skipped — {no_mirror}")
    elif has_mirror:
        # linear-found is not rebuilt: every stub a notion record now resolves to hands
        # over here, so a `generate` before the next build clears nothing in Linear
        _handover(args, out_dir, store, stub_ids(entries, store.table), set(), entries)
    for msg in store.reports:
        print(f"  NOTE: {msg}")
    added = len(store.table.entries) - before
    if args.dry_run:
        print(f"DRY RUN — identities: would append {added} new; {IDENTITIES_FILE} not written.")
        return 0
    saved = store.save()
    print(f"identities: {added} new; {IDENTITIES_FILE} {'updated' if saved else 'unchanged'}")
    print(f"run generate to refresh {out_dir / DESIRED_SUBDIR}/ and the CSVs")
    return 0


def _cmd_generate(args) -> int:
    """Datasets merged by identity → `linear/` (with its manifest) and both CSVs."""
    out_dir = _out_dir(args)
    errors = override_errors(out_dir / OVERRIDES_FILE, OVERRIDES_FORMAT, LINEAR_SCHEMA)
    if not errors:
        errors = value_type_failures(load_overrides_file(out_dir))
    if errors:
        print(f"ERROR: {OVERRIDES_FILE} is invalid; nothing written:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 2
    generated = generate_from_dir(out_dir)
    recs = generated.rows
    if not generated.winners:
        print(
            f"WARNING: no dataset records under {out_dir}/{DATASETS_SUBDIR}/ (missing or empty); "
            "run `ingest` first.",
            file=sys.stderr,
        )
    print(
        f"generate: {len(recs)} Linear projections "
        f"({Counter(r.status for r in recs)}); {len(generated.winners)} registry rows"
    )
    if args.dry_run:
        print(
            f"DRY RUN — would write {len(recs)} files under {out_dir}/{DESIRED_SUBDIR}/ "
            f"+ {REGISTRY_CSV} + {REAL_CUSTOMERS_CSV}."
        )
        return 0
    if not _guard_or_refuse(out_dir / DESIRED_SUBDIR, "generate", args):
        return 2
    write_desired(recs, out_dir)
    write_manifest(out_dir / DESIRED_SUBDIR, "generate")
    write_csvs(generated.winners, out_dir)
    print(f"wrote {out_dir}/{DESIRED_SUBDIR}/*.yaml + {REGISTRY_CSV} + {REAL_CUSTOMERS_CSV}")
    return 0


def _linear_client():
    if not os.environ.get("LINEAR_API_TOKEN"):
        print("ERROR: LINEAR_API_TOKEN not set (set it in the environment).", file=sys.stderr)
        return None
    from .linear_client import LinearClient

    return LinearClient()


def _cmd_sync_inbound(args) -> int:
    """Live Linear → the state mirror; the Linear edits since the last mirror →
    linear-overrides.yaml (`inbound`). `--dry-run` reads and counts, writes nothing."""
    out_dir = _out_dir(args)
    if legacy_refused(out_dir):
        return 2
    client = _linear_client()
    if client is None:
        return 2
    try:
        result = read_inbound(out_dir, LinearDestination(client), scope=args.scope)
    except ValueError as e:
        for line in str(e).splitlines():
            print(f"ERROR: {line}", file=sys.stderr)
        return 2
    except DestinationError as e:
        print(f"ERROR: Linear read failed: {e}; nothing written.", file=sys.stderr)
        return 2
    mirrored = len(scope_recs(result.mirror, args.scope))
    pending = sum(1 for e in result.edits if e.kind == "pending")
    absorbed = len(result.edits) - pending
    print(f"sync-inbound: {mirrored} mirrored; {absorbed} absorbed, {pending} pending")
    for s in result.settled:
        what = f"{s.field}{PENDING_ADD} {', '.join(s.values)}" if s.values else s.field
        print(f"  NOTE: {s.canonical_id}: pending {what} dropped: the registry now agrees")
    for cid, live_id in result.deletions.confirmed:
        print(f"  {cid}: deleted in Linear ({live_id}); recorded as deleted")
    for cid, live_id in result.deletions.still_live:
        print(
            f"  NOTE: {cid}: missing from the Linear listing but found by id ({live_id}); "
            "nothing recorded"
        )
    if args.dry_run:
        print(
            f"DRY RUN — would write {len(result.mirror)} files under {out_dir}/{STATE_SUBDIR}/ "
            f"and record {len(result.edits)} edits and "
            f"{len(result.deletions.confirmed)} deletions in {OVERRIDES_FILE}."
        )
        return 0
    if not _guard_or_refuse(out_dir / STATE_SUBDIR, "sync-inbound", args):
        return 2
    changed = write_inbound(out_dir, result, today=datetime.now(UTC).date().isoformat())
    print(f"wrote {out_dir}/{STATE_SUBDIR}/*.yaml; {OVERRIDES_FILE}: {changed} changes")
    return 0


def _render_change(c: FieldChange) -> str:
    if c.field == "external_ids":
        return f"    external_ids: [{', '.join(c.old)}] -> [{', '.join(c.new)}]"
    if c.field == "domains" and c.kind == "add":
        return f"    domains: + {c.new}"
    if c.field == "domains" and c.kind == "remove":
        return f"    domains: - {c.old}"
    if c.kind == "clear":
        return f"    {c.field}: {c.old!r} -> None"
    return f"    {c.field}: {c.old!r} -> {c.new!r}"


def render_plan(plan) -> list[str]:
    """Terraform-style plan lines: creates, then updates field by field, in plan order."""
    lines = [f"+ {c.name}" for c in plan.creates]
    for target, lv in plan.updates:
        changes = plan.changes.get(lv.id, [])
        suffix = " (retire)" if any(c.retire for c in changes) else ""
        lines.append(f"~ {target.name}{suffix}")
        lines += [_render_change(c) for c in changes]
    return lines


def _print_plan(plan) -> None:
    destructive = sum(1 for cs in plan.changes.values() for c in cs if c.kind in DESTRUCTIVE)
    print(
        f"DeployPlan: creates={len(plan.creates)} updates={len(plan.updates)} "
        f"aborts={len(plan.aborts)} orphans={len(plan.orphans)} unchanged={len(plan.unchanged)} "
        f"destructive={destructive}"
    )
    for line in render_plan(plan):
        print(line)
    if plan.aborts:
        for d, _, reason in plan.aborts:
            print(f"  ABORT: {d.name} — {reason}")
    if plan.orphans:
        print("  ORPHAN (report-only, not deleted):", ", ".join(o.name for o in plan.orphans))
    for cid, lv in plan.known_duplicates:
        print(
            f"  KNOWN DUPLICATE: {cid}: '{lv.name}' ({lv.id}) is never written; merge it in Linear"
        )
    for msg in plan.withheld:
        print(f"  WITHHELD: {msg}")
    for msg in plan.notes:
        print(f"  NOTE: {msg}")
    for msg in plan.errors:
        print(f"  ERROR: {msg}")


def _refresh_state(persisted: list, out_dir: Path) -> None:
    """Merge freshly-written rows (with new linear_synced) into the state mirror.
    A full `sync-inbound` is still required to make state == live (unchanged
    rows and orphans are not touched here)."""
    state = load_state(out_dir)
    by_id = {s.linear_synced.linear_id: i for i, s in enumerate(state) if s.linear_synced}
    for p in persisted:
        i = by_id.get(p.linear_synced.linear_id)
        if i is None:
            state.append(p)
        else:
            state[i] = p
    state_dir = out_dir / STATE_SUBDIR
    edits = manifest_edits(state_dir)
    if edits:
        # The apply was already approved, so replace without asking; say what is lost.
        print(
            f"WARNING: {state_dir} had hand edits ({', '.join(edits)}); "
            "overwriting with the applied state.",
            file=sys.stderr,
        )
    write_state(state, out_dir)
    write_manifest(state_dir, "sync-inbound")


def _prod_write_refused(args, what: str) -> bool:
    """A --prod write outside GitHub Actions needs --break-glass, which warns and
    proceeds (the apply still snapshots)."""
    if not args.prod or os.environ.get("GITHUB_ACTIONS") == "true":
        return False
    if not args.break_glass:
        flags = "--apply --prod" if what == "deploy" else "restore --prod"
        print(f"ERROR: {flags} outside GitHub Actions needs --break-glass", file=sys.stderr)
        return True
    print(
        f"WARNING: --break-glass: {what} --prod from outside GitHub Actions. "
        "This bypasses the reviewed deploy workflow.",
        file=sys.stderr,
    )
    return False


def _cmd_deploy(args) -> int:
    client = _linear_client()
    if client is None:
        return 2
    out_dir = _out_dir(args)
    inp = load_inputs(out_dir, args.scope, require_desired=True)
    if inp is None:
        return 2
    planning = plan_for_deploy(inp, client.list_customers(), args.scope)
    plan, conflicts, reports = planning.plan, planning.conflicts, planning.reports
    _print_plan(plan)
    for msg in reports:
        print(f"  NOTE: {msg}")
    if conflicts:
        for msg in conflicts:
            print(f"  ERROR: {msg}")
        print(
            f"REFUSED — hidden conflict(s) above; nothing written. Fix {OVERRIDES_FILE} "
            "or the desired record, then re-run."
        )
        return 3
    if plan.errors:
        print(
            f"REFUSED — planner error(s) above; nothing written. Fix {IDENTITIES_FILE} "
            "or merge the duplicates in Linear, then re-run."
        )
        return 3
    if plan.unacked:
        for msg in plan.unacked:
            print(f"  UNACKNOWLEDGED DESTRUCTIVE: {msg}")
        print("REFUSED — unacknowledged destructive change(s); nothing written.")
        return 3
    if not args.apply:
        print(
            "DRY RUN — no writes. Re-run with --apply to deploy creates+updates "
            "(orphans are never auto-deleted)."
        )
        return 3 if plan.aborts else 0
    # Hard guard: the configured data_dir is production; require an explicit opt-in.
    if _is_prod(args, out_dir) and not args.prod:
        print("ERROR: --apply against the configured data_dir requires --prod.", file=sys.stderr)
        return 2
    if _prod_write_refused(args, "deploy"):
        return 2
    try:
        result = apply_deploy(
            plan,
            client,
            snapshot_dir=out_dir / SNAPSHOT_SUBDIR,
            exclude=hidden_reason(inp.hidden, inp.table, scope=args.scope),
            scope=args.scope,
        )
    except (OSError, ValueError) as e:
        print(f"ERROR: apply refused before any write: {e}", file=sys.stderr)
        return 2
    _refresh_state(result.persisted, out_dir)
    for d, _, reason in plan.aborts:
        print(f"  ABORT: {d.name} — {reason}")
    if result.failure:
        print(f"  FAILED: {result.failure}")
    print(
        f"APPLIED: {len(result.persisted)} written; {len(plan.aborts)} aborted. "
        f"Refreshed linear_synced in {out_dir}/{STATE_SUBDIR}/ "
        "(run sync-inbound to fully reconcile)."
    )
    print(f"snapshot: {result.snapshot}")
    print(f"restore: customer-registry-sync restore --from {result.snapshot} --prod")
    return 3 if result.failure or plan.aborts else 0


def _cmd_restore(args) -> int:
    out_dir = _out_dir(args)
    if _is_prod(args, out_dir) and not args.prod:
        print("ERROR: restore against the configured data_dir requires --prod.", file=sys.stderr)
        return 2
    if _prod_write_refused(args, "restore"):
        return 2
    client = _linear_client()
    if client is None:
        return 2
    try:
        res = restore(args.from_, client, rows=args.rows)
    except (OSError, ValueError) as e:
        print(f"ERROR: restore refused: {e}", file=sys.stderr)
        return 2
    print(f"restored: {len(res.restored)}; skipped: {len(res.skipped)} (from {args.from_})")
    for lid in res.restored:
        print(f"  RESTORED: {lid}")
    for lid, reason in res.skipped:
        print(f"  SKIPPED: {lid} — {reason}")
    if res.failure:
        print(f"  FAILED: {res.failure} (later rows not attempted)")
        return 2
    return 3 if res.skipped else 0


def _print_drift(drift) -> None:
    for s, _lv in drift.changed:
        print(f"  DRIFT changed: {s.name} (state {s.linear_synced.content_hash[:8]} != live)")
    for lv in drift.added:
        print(f"  DRIFT added: {lv.name} (live {lv.id}, no state row)")
    for s in drift.deleted:
        print(f"  DRIFT deleted: {s.name} (state {s.linear_synced.linear_id}, not live)")


def _cmd_check(args) -> int:
    out_dir = _out_dir(args)
    if legacy_refused(out_dir):
        return EXIT_OPERROR
    # a malformed overrides file fails check before Linear is read
    malformed = override_errors(out_dir / OVERRIDES_FILE, OVERRIDES_FORMAT, LINEAR_SCHEMA)
    if malformed:
        for msg in malformed:
            print(f"  OVERRIDE: {msg}")
        print(f"check FAILED: {OVERRIDES_FILE} is invalid; fix the OVERRIDE lines above")
        return EXIT_DRIFT
    client = _linear_client()
    if client is None:
        return EXIT_OPERROR
    inp = load_inputs(out_dir, args.scope)
    if inp is None:
        return EXIT_OPERROR
    try:
        all_live = client.list_customers()
    except LinearError as e:
        print(f"ERROR: Linear read failed: {e}", file=sys.stderr)
        return EXIT_OPERROR
    res = check_for(inp, all_live, args.scope)
    findings = override_findings(out_dir)
    drift, plan = res.drift, res.plan
    for line in hidden_lines(res.ignored, inp.hidden, inp.table, scope=args.scope):
        print(f"  {line}")
    print(
        f"check: drift changed={len(drift.changed)} added={len(drift.added)} "
        f"deleted={len(drift.deleted)}"
    )
    _print_drift(drift)
    # failure lines first, then the plan, then what is only reported
    for msg in plan.unacked:
        print(f"  UNACKNOWLEDGED DESTRUCTIVE: {msg}")
    for lv in res.orphans:
        first, *rest = orphan_lines(lv)
        print(f"  ERROR: {first}")
        for line in rest:
            print(f"  {line}")
    for msg in res.failures:
        print(f"  ERROR: {msg}")
    for p in findings.pending:
        for line in pending_lines(p):
            print(f"  {line}")
    for msg in findings.failures:
        print(f"  OVERRIDE: {msg}")
    _print_plan(plan)
    for msg in [*res.reports, *findings.reports]:
        print(f"  NOTE: {msg}")
    offline = offline_errors(out_dir)
    for msg in offline:
        print(f"  OFFLINE: {msg}")
    code = check_exit(res, offline, findings)
    if code == 0:
        print("IN SYNC")
    elif res.code or findings.failures or findings.pending:
        print("check FAILED: see the ERROR, DRIFT, PENDING, OVERRIDE and OFFLINE lines above")
    else:
        print("OFFLINE CHECK FAILED — run the named generator, commit, re-run")
    return code


LINEAR_TOKEN_ENV = "LINEAR_API_TOKEN"

# What `refresh` runs, in order: the state mirror, the found-dataset ingest, the generated files.
REFRESH_STEPS: tuple[tuple[str, ...], ...] = (
    ("sync-inbound", "--no-input"),
    ("ingest", "--only", LINEAR_FOUND, "--no-input"),
    ("generate", "--no-input"),
)


def _cmd_refresh(args) -> int:
    """`sync-inbound`, `ingest --only linear-found`, `generate`; the first non-zero exit
    stops the run and is returned. Reads Linear only (never `check`, never a write).

    Only `sync-inbound` talks to Linear, so the token is withdrawn from the environment
    for the offline steps and restored afterwards. A legacy file refuses the run before
    any step."""
    if legacy_refused(args.registry.data_dir):
        return 2
    parser, handlers = build_parser()
    token = os.environ.get(LINEAR_TOKEN_ENV)
    try:
        for step in REFRESH_STEPS:
            step_args = parser.parse_args(["--config", str(args.config), *step])
            step_args.registry = args.registry
            code = handlers[step_args.command](step_args)
            os.environ.pop(LINEAR_TOKEN_ENV, None)
            if code != 0:
                return code
    finally:
        if token is not None:
            os.environ[LINEAR_TOKEN_ENV] = token
    return 0


def _cmd_adopt_live(args) -> int:
    """Migrate a v0.2.0 data dir to full ownership, offline (`migrate.adopt_live`): keep
    today's Linear-only values as overrides and turn the ignore file into hidden
    entries. Exit 2, writing nothing, when the data dir cannot be migrated."""
    out_dir = _out_dir(args)
    try:
        result = adopt_live(out_dir, dry_run=args.dry_run)
    except AdoptRefused as e:
        for line in str(e).splitlines():
            print(f"ERROR: {line}", file=sys.stderr)
        print("adopt-live refused; nothing written.", file=sys.stderr)
        return 2
    print(
        f"adopt-live: {result.pins} pins, {result.adds} domain adds, {result.hidden} hidden, "
        f"{result.prefixes} hidden prefixes, {result.identities_added} identities added"
    )
    if args.dry_run:
        print(f"DRY RUN — nothing written under {out_dir}/.")
        return 0
    print("next: run generate, then check")
    return 0


def _cmd_changed(args) -> int:
    """Print `changed` or `unchanged` for the deploy inputs since a sha; always exit 0."""
    try:
        changed = changed_since(Path.cwd(), args.since, deploy_inputs(args.registry))
    except ValueError:  # not a git repository, or data_dir outside it: any doubt is "changed"
        changed = True
    print("changed" if changed else "unchanged")
    return 0


def _cmd_config_get(args) -> int:
    """Print one config value (dotted key); exit 2 naming a missing or unknown key."""
    try:
        print(config_value(args.registry, args.key))
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_tool_source(args) -> int:
    """Print where the installed tool came from; exit 2 when it cannot be recovered."""
    try:
        print(tool_source().line())
    except ToolSourceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


def _out_dir_arg(p) -> None:
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="data directory (default: data_dir from the config)",
    )


def _no_input_arg(p, help_text="never prompt; refuse to overwrite hand-edited files") -> None:
    p.add_argument("--no-input", action="store_true", help=help_text)


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    """The argument parser and the handler for each subcommand.

    Split out of `main` so tests can check the invocations in the workflow against it.
    """
    parser = argparse.ArgumentParser(prog="customer-registry-sync")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(DEFAULT_CONFIG_NAME),
        help=f"registry config file (default: {DEFAULT_CONFIG_NAME} in the cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    handlers = {}

    ing = sub.add_parser("ingest", help="Notion + linear-found (mirror) datasets")
    ing.add_argument(
        "--only",
        choices=[NOTION, LINEAR_FOUND],
        default=None,
        help="build one dataset; linear-found needs no Notion token",
    )
    ing.add_argument("--dry-run", action="store_true")
    _out_dir_arg(ing)
    _no_input_arg(ing)
    handlers["ingest"] = _cmd_ingest

    gen = sub.add_parser("generate", help="datasets → Layer-2 <data_dir>/linear/* + CSVs")
    gen.add_argument("--dry-run", action="store_true")
    _out_dir_arg(gen)
    _no_input_arg(gen)
    handlers["generate"] = _cmd_generate

    syn = sub.add_parser("sync-inbound", help="live Linear → Layer-2 mirror")
    syn.add_argument("--dry-run", action="store_true")
    _out_dir_arg(syn)
    syn.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    _no_input_arg(syn)
    handlers["sync-inbound"] = _cmd_sync_inbound

    dep = sub.add_parser("deploy", help="Layer-2 → Linear (dry-run unless --apply)")
    dep.add_argument("--apply", action="store_true", help="perform live Linear writes")
    _out_dir_arg(dep)
    dep.add_argument(
        "--prod",
        action="store_true",
        help="required with --apply when --out-dir is the configured data_dir",
    )
    dep.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    dep.add_argument(
        "--break-glass",
        action="store_true",
        help="allow --apply --prod outside GitHub Actions (prints a warning)",
    )
    _no_input_arg(dep, "never prompt (deploy never does)")
    handlers["deploy"] = _cmd_deploy

    rst = sub.add_parser("restore", help="write a pre-apply snapshot back to Linear")
    rst.add_argument(
        "--from",
        dest="from_",
        type=Path,
        required=True,
        help=f"snapshot file (<data_dir>/{SNAPSHOT_SUBDIR}/<timestamp>.yaml)",
    )
    rst.add_argument(
        "--rows",
        nargs="+",
        default=None,
        metavar="XID",
        help="only rows carrying one of these external ids",
    )
    _out_dir_arg(rst)
    rst.add_argument(
        "--prod",
        action="store_true",
        help="required when --out-dir is the configured data_dir",
    )
    rst.add_argument(
        "--break-glass",
        action="store_true",
        help="allow --prod outside GitHub Actions (prints a warning)",
    )
    _no_input_arg(rst, "never prompt (restore never does)")
    handlers["restore"] = _cmd_restore

    ref = sub.add_parser(
        "refresh",
        help="sync-inbound, ingest --only linear-found, generate (reads Linear; no Linear writes)",
    )
    _no_input_arg(ref, "never prompt; refuse to overwrite hand-edited files")
    handlers["refresh"] = _cmd_refresh

    adopt = sub.add_parser(
        "adopt-live",
        help="migrate a v0.2.0 data dir: keep Linear-only values, convert the ignore file",
    )
    adopt.add_argument("--dry-run", action="store_true")
    _out_dir_arg(adopt)
    _no_input_arg(adopt, "never prompt (adopt-live never does)")
    handlers["adopt-live"] = _cmd_adopt_live

    chk = sub.add_parser("check", help="plan guard: drift (state vs live) + plan; exit 0/2/3")
    _out_dir_arg(chk)
    chk.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    handlers["check"] = _cmd_check

    chg = sub.add_parser(
        "changed",
        help="print changed/unchanged: did any deploy input change since <sha>? (exit 0)",
    )
    chg.add_argument("--since", required=True, help="the commit to compare HEAD against")
    handlers["changed"] = _cmd_changed

    cfg_get = sub.add_parser(
        "config-get", help="print one config value, e.g. data_dir or deploy_environment"
    )
    cfg_get.add_argument("key", help="e.g. data_dir, readonly_environment, state_dir, e2e_tests")
    handlers["config-get"] = _cmd_config_get

    sub.add_parser(
        "tool-source",
        help="print the installed tool's source: 'git <commit> <url>' or 'dir - <path>'",
    )
    handlers["tool-source"] = _cmd_tool_source
    return parser, handlers


def main(argv: list[str] | None = None) -> int:
    parser, handlers = build_parser()
    args = parser.parse_args(argv)
    try:
        args.registry = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
