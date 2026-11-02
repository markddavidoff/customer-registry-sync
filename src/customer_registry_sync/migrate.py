"""`adopt-live`: migrate a v0.2.0 data repo to full ownership, offline and with no token.

Under full ownership a deploy clears every revenue and tier the registry lacks and
removes every domain it does not list. `adopt_live` keeps today's Linear values instead:
for each managed, paired, non-stub customer in the committed state mirror, it records
the live values the new rules would otherwise remove as accepted overrides (pins and
domain adds in `linear-overrides.yaml`). It also turns `linear-ignore.yaml` into
`hidden` entries and `hidden_name_prefixes`, then deletes it, and deletes the
`linear-retire.yaml` that must already be empty.

This is the one module that still reads the ignore file (`gitops_engine.ignore`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from gitops_engine.hidden import hidden_from
from gitops_engine.ignore import ignore_errors, load_ignore
from gitops_engine.manifest import MANIFEST
from gitops_engine.overrides import OverridesStore, adopt_entry, override_errors
from gitops_engine.retire import load_retire

from .datasets import load_datasets, load_datasets_config
from .deploy_plan import state_to_live
from .generate import merge_datasets
from .identity import IDENTITIES_FILE, SOURCE_LINEAR, IdentityStore, pair_rows, strip_status_suffix
from .linear_files import IGNORE_FILE, IGNORE_FORMAT, RETIRE_FILE, partition_hidden
from .linear_overrides import OVERRIDES_FILE, OVERRIDES_FORMAT, uncurated
from .linear_schema import LINEAR_SCHEMA
from .schema import CustomerRecord
from .writers import STATE_SUBDIR, load_state, slug_for


@dataclass(frozen=True)
class AdoptResult:
    pins: int  # pinned live revenue and tier values
    adds: int  # live domains added
    hidden: int  # ignore entries now hidden
    prefixes: int  # ignore-file name prefixes now hidden prefixes
    identities_added: int  # identities made for ignored customers that had none


class AdoptRefused(ValueError):
    """The data repo cannot be migrated as it stands; nothing was written."""


def identity_for_live(store: IdentityStore, live_id: str, name: str) -> tuple[str, bool]:
    """(canonical id, created): the identity whose `sources.linear` is `live_id`, else a
    new one carrying it, keyed by a slug of `name` (status suffix stripped), or by
    `live_id` when the name has nothing to slug; `-2`, `-3` make it unique."""
    for cid, ident in store.table.entries.items():
        if ident.sources.get(SOURCE_LINEAR) == live_id:
            return cid, False
    base = slug_for(CustomerRecord(canonical_name=strip_status_suffix(name), domains=[]))
    return store.add_identity(base or live_id, sources={SOURCE_LINEAR: live_id}), True


def _refusals(data_dir: Path) -> list[str]:
    """Why `data_dir` cannot be migrated: a retire file with entries, no state mirror,
    or an identity, ignore, overrides or retire file that cannot be read."""
    out: list[str] = []
    try:
        if load_retire(data_dir / RETIRE_FILE, LINEAR_SCHEMA):
            out.append(
                f"{RETIRE_FILE} has entries: apply and remove them under v0.2.0 first, "
                "then empty the file"
            )
    except (yaml.YAMLError, AttributeError, TypeError, ValueError) as e:  # not a mapping
        out.append(f"{RETIRE_FILE} cannot be read: {e}")
    if not (data_dir / STATE_SUBDIR / MANIFEST).is_file():
        out.append(f"no state mirror at {data_dir / STATE_SUBDIR}: run sync-inbound first")
    out += ignore_errors(data_dir / IGNORE_FILE, IGNORE_FORMAT)
    out += override_errors(data_dir / OVERRIDES_FILE, OVERRIDES_FORMAT, LINEAR_SCHEMA)
    try:
        IdentityStore(data_dir / IDENTITIES_FILE)
    except ValueError as e:
        out.append(str(e))
    return out


def adopt_live(data_dir: Path, *, dry_run: bool = False) -> AdoptResult:
    """Migrate `data_dir` (see the module docstring); with `dry_run`, write nothing.

    Raises `AdoptRefused`, before writing anything, when `linear-retire.yaml` has
    entries, the state mirror is missing, or a curated file cannot be read."""
    data_dir = Path(data_dir)
    refusals = _refusals(data_dir)
    if refusals:
        raise AdoptRefused("\n".join(refusals))
    identities = IdentityStore(data_dir / IDENTITIES_FILE)
    store = OverridesStore(data_dir / OVERRIDES_FILE, OVERRIDES_FORMAT)
    state = [r for r in load_state(data_dir) if r.linear_synced is not None]
    mirror = [state_to_live(r) for r in state]
    names = {lv.id: lv.name for lv in mirror}

    # 1. the ignore file -> hidden entries and hidden prefixes (the built-in stays built in)
    ignore = load_ignore(data_dir / IGNORE_FILE, IGNORE_FORMAT)
    hidden = created = prefixes = 0
    for e in ignore.entries:
        cid, new = identity_for_live(identities, e.live_id, e.name or names.get(e.live_id, ""))
        created += new
        hidden += store.hide(cid, e.reason)
    for p in ignore.prefixes:
        if p.line is not None:
            prefixes += store.add_hidden_prefix(p.prefix, p.reason)

    # 2. the desired rows with the (unsaved) overrides, paired with the managed mirror rows
    cfg = load_datasets_config(data_dir)
    entries = load_datasets(data_dir, cfg)
    desired = merge_datasets(entries, identities.table, cfg.names, overrides=store.overrides).rows
    managed, _ = partition_hidden(
        mirror, hidden_from(store.overrides), identities.table, scope=None
    )

    # 3. keep each live value the new rules would clear or remove
    pins = adds = 0
    for d, lv in pair_rows(desired, managed, identities.table).pairs:
        if lv is None or LINEAR_SCHEMA.destination_owned(d):
            continue
        entry = adopt_entry(d, lv, schema=LINEAR_SCHEMA)
        cid = d.external_ids[0]
        current = store.overrides.entries.get(cid)
        if entry is not None and current is not None:
            entry = uncurated(entry, current)
        if entry is None:
            continue
        store.adopt(cid, entry)
        pins += len(entry.pin)
        adds += sum(len(values) for values in entry.add.values())

    if not dry_run:
        store.save()
        identities.save()
        for name in (IGNORE_FILE, RETIRE_FILE):
            (data_dir / name).unlink(missing_ok=True)
    return AdoptResult(pins, adds, hidden, prefixes, created)
