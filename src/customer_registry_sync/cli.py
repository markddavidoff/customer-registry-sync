"""CLI for the customer registry.

Global option ``--config <path>`` names the YAML config file (default
``registry-sync.yaml`` in the working directory); see ``config.py``.

Phase A:  ``ingest``        — Notion hub pages → Layer-1 dataset.
Phase B:  ``generate``      — Layer-1 dataset → Layer-2 ``<data_dir>/linear/*``.
          ``sync-inbound``  — live Linear → Layer-2 mirror (refresh linear_synced).
          ``deploy``        — Layer-2 → Linear, per-customer check-and-set.
                              DRY-RUN by default; ``--apply`` performs live writes.
Phase C:  ``check``         — read-only gate: drift (state vs live) + plan; exit 0/2/3.

Layer 1 is the datasets (``<data_dir>/datasets/<name>/``); Layer 2 is the Linear projection:
desired customers (``linear/``) and the state mirror of live Linear (``linear-state/``).

Secrets come from the environment: ``NOTION_API_TOKEN`` (ingest), ``LINEAR_API_TOKEN`` (Phase B).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from .assemble import assemble_dataset
from .check import EXIT_OPERROR, run_check
from .config import DEFAULT_CONFIG_NAME, ConfigError, load_config
from .datasets import NOTION, dataset_dir
from .deploy import apply_deploy, plan_deploy
from .generate import generate_linear
from .inbound_sync import reconcile_inbound
from .inputs import load_overrides, load_self_managed_customers
from .linear_client import LinearError
from .notion_ingest import extract_customers
from .writers import (
    REAL_CLASSIFICATIONS,
    load_dataset,
    load_desired,
    load_state,
    write_dataset,
    write_desired,
    write_state,
)


def _out_dir(args) -> Path:
    """``--out-dir`` if given, else the config's ``data_dir``."""
    return args.out_dir if args.out_dir is not None else args.registry.data_dir


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


def _cmd_ingest(args) -> int:
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        print("ERROR: NOTION_API_TOKEN not set (set it in the environment).", file=sys.stderr)
        return 2
    from notion_client import Client  # lazy so unit tests need no network

    out_dir = _out_dir(args)
    self_managed_path = out_dir / "self-managed-customers.yaml"
    overrides_path = out_dir / "classification-overrides.yaml"
    raw = extract_customers(Client(auth=token), args.registry.buckets)
    self_managed = (
        load_self_managed_customers(self_managed_path) if self_managed_path.exists() else []
    )
    overrides = load_overrides(overrides_path) if overrides_path.exists() else {}
    records = assemble_dataset(raw, self_managed, overrides)
    print(_dataset_summary(records))
    if args.dry_run:
        print(f"DRY RUN — would write {len(records)} dataset files under {out_dir}/datasets/{NOTION}/.")
        return 0
    write_dataset(records, out_dir)
    print(f"wrote {dataset_dir(out_dir, NOTION)}/*.yaml")
    return 0


def _cmd_generate(args) -> int:
    out_dir = _out_dir(args)
    dataset = load_dataset(out_dir)
    if not dataset:
        print(
            f"WARNING: no dataset records under {dataset_dir(out_dir, NOTION)} (missing or empty); "
            "run `ingest` first.",
            file=sys.stderr,
        )
    recs = generate_linear(dataset)
    print(f"generate: {len(recs)} Linear projections ({Counter(r.status for r in recs)})")
    if args.dry_run:
        print(f"DRY RUN — would write {len(recs)} files under {out_dir}/linear/.")
        return 0
    write_desired(recs, out_dir)
    print(f"wrote {out_dir}/linear/*.yaml")
    return 0


def _linear_client():
    if not os.environ.get("LINEAR_API_TOKEN"):
        print("ERROR: LINEAR_API_TOKEN not set (set it in the environment).", file=sys.stderr)
        return None
    from .linear_client import LinearClient

    return LinearClient()


def _scope_live(live, scope: str | None):
    return [c for c in live if c.name.startswith(scope)] if scope else live


def _scope_recs(recs, scope: str | None):
    return [r for r in recs if r.name.startswith(scope)] if scope else recs


def _cmd_sync_inbound(args) -> int:
    client = _linear_client()
    if client is None:
        return 2
    out_dir = _out_dir(args)
    existing = load_state(out_dir)
    live = _scope_live(client.list_customers(), args.scope)
    recs = reconcile_inbound(live, _scope_recs(existing, args.scope))
    if args.scope:
        # Scoped run: replace only the in-scope rows; out-of-scope state is untouched.
        recs = [r for r in existing if not r.name.startswith(args.scope)] + recs
    print(f"sync-inbound: {len(live)} live Linear customers mirrored")
    if args.dry_run:
        print(f"DRY RUN — would write {len(recs)} files under {out_dir}/linear-state/.")
        return 0
    write_state(recs, out_dir)
    print(f"wrote {out_dir}/linear-state/*.yaml")
    return 0


def _print_plan(plan) -> None:
    print(
        f"DeployPlan: creates={len(plan.creates)} updates={len(plan.updates)} "
        f"aborts={len(plan.aborts)} orphans={len(plan.orphans)} unchanged={len(plan.unchanged)}"
    )
    if plan.creates:
        print("  CREATE:", ", ".join(c.name for c in plan.creates))
    if plan.updates:
        print("  UPDATE:", ", ".join(d.name for d, _ in plan.updates))
    if plan.aborts:
        for d, _, reason in plan.aborts:
            print(f"  ABORT: {d.name} — {reason}")
    if plan.orphans:
        print("  ORPHAN (report-only, not deleted):", ", ".join(o.name for o in plan.orphans))


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
    write_state(state, out_dir)


def _cmd_deploy(args) -> int:
    client = _linear_client()
    if client is None:
        return 2
    out_dir = _out_dir(args)
    desired = _scope_recs(load_desired(out_dir), args.scope)
    if not desired and not args.scope:
        print("No <data_dir>/linear/* found — run `generate` first.", file=sys.stderr)
        return 2
    state = _scope_recs(load_state(out_dir), args.scope)
    live = _scope_live(client.list_customers(), args.scope)
    plan = plan_deploy(desired, live, state=state)
    _print_plan(plan)
    if not args.apply:
        print(
            "DRY RUN — no writes. Re-run with --apply to deploy creates+updates "
            "(orphans are never auto-deleted)."
        )
        return 3 if plan.aborts else 0
    # Hard guard: the configured data_dir is production; require an explicit opt-in.
    if out_dir.resolve() == args.registry.data_dir.resolve() and not args.prod:
        print("ERROR: --apply against the configured data_dir requires --prod.", file=sys.stderr)
        return 2
    persisted = apply_deploy(plan, client)
    _refresh_state(persisted, out_dir)
    print(
        f"APPLIED: {len(persisted)} writes; {len(plan.aborts)} aborted. "
        f"Refreshed linear_synced in {out_dir}/linear-state/ (run sync-inbound to fully reconcile)."
    )
    return 3 if plan.aborts else 0


def _print_drift(drift) -> None:
    for s, _lv in drift.changed:
        print(f"  DRIFT changed: {s.name} (state {s.linear_synced.content_hash[:8]} != live)")
    for lv in drift.added:
        print(f"  DRIFT added: {lv.name} (live {lv.id}, no state row)")
    for s in drift.deleted:
        print(f"  DRIFT deleted: {s.name} (state {s.linear_synced.linear_id}, not live)")


def _cmd_check(args) -> int:
    client = _linear_client()
    if client is None:
        return EXIT_OPERROR
    out_dir = _out_dir(args)
    desired = _scope_recs(load_desired(out_dir), args.scope)
    state = _scope_recs(load_state(out_dir), args.scope)
    try:
        live = _scope_live(client.list_customers(), args.scope)
    except LinearError as e:
        print(f"ERROR: Linear read failed: {e}", file=sys.stderr)
        return EXIT_OPERROR
    # No alias file any more; `run_check` loses its `aliases` parameter with the planner port.
    code, drift, plan = run_check(desired, state, live, {})
    print(
        f"check: drift changed={len(drift.changed)} added={len(drift.added)} deleted={len(drift.deleted)}"
    )
    _print_drift(drift)
    _print_plan(plan)
    print("IN SYNC" if code == 0 else "DRIFT — run sync-inbound, resolve, re-run")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="customer-registry-sync")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(DEFAULT_CONFIG_NAME),
        help=f"registry config file (default: {DEFAULT_CONFIG_NAME} in the cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    handlers = {}

    ing = sub.add_parser("ingest", help="Notion hub pages → Layer-1 dataset")
    ing.add_argument("--dry-run", action="store_true")
    ing.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="dataset directory (default: data_dir from the config)",
    )
    handlers["ingest"] = _cmd_ingest

    gen = sub.add_parser("generate", help="Layer-1 dataset → Layer-2 <data_dir>/linear/*")
    gen.add_argument("--dry-run", action="store_true")
    gen.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="dataset directory (default: data_dir from the config)",
    )
    handlers["generate"] = _cmd_generate

    syn = sub.add_parser("sync-inbound", help="live Linear → Layer-2 mirror")
    syn.add_argument("--dry-run", action="store_true")
    syn.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="dataset directory (default: data_dir from the config)",
    )
    syn.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    handlers["sync-inbound"] = _cmd_sync_inbound

    dep = sub.add_parser("deploy", help="Layer-2 → Linear (dry-run unless --apply)")
    dep.add_argument("--apply", action="store_true", help="perform live Linear writes")
    dep.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="dataset directory (default: data_dir from the config)",
    )
    dep.add_argument(
        "--prod",
        action="store_true",
        help="required with --apply when --out-dir is the configured data_dir",
    )
    dep.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    handlers["deploy"] = _cmd_deploy

    chk = sub.add_parser("check", help="read-only gate: drift (state vs live) + plan; exit 0/2/3")
    chk.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="dataset directory (default: data_dir from the config)",
    )
    chk.add_argument("--scope", default=None, help="only rows whose name starts with this prefix")
    handlers["check"] = _cmd_check

    args = parser.parse_args(argv)
    try:
        args.registry = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
