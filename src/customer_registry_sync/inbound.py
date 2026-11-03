"""`sync-inbound`: refresh the state mirror from live Linear and record the Linear edits
since the last mirror in `linear-overrides.yaml`.

`read_inbound` reads everything and decides; it writes nothing. The edits are the
engine's (`gitops_engine.edits.detect_edits`): the old mirror against live Linear,
field by field, on the managed (not hidden), in-scope customers, with names compared
without their ` [Status]` suffix (`base_value`). Stubs are destination-owned and never
pair, so a stub's Linear edit records nothing. A pending entry whose decision is moot
(Linear's value now equals the registry's) is `settled`, and `write_inbound` drops it.

A managed customer in the old mirror that is missing from the full listing is a
deletion only when a direct `get` by its Linear id also reports it gone
(`gitops_engine.deletions.confirm_deletions`); `write_inbound` records it as `deleted`.
A Linear error in the listing or in a `get` propagates, so nothing is written.

`write_inbound` writes `linear-state/` and its manifest, then the overrides file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gitops_engine.deletions import Deletions, confirm_deletions
from gitops_engine.destination import Destination
from gitops_engine.edits import Edit, detect_edits, pair_old_state, record_edits
from gitops_engine.hidden import hidden_from
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror
from gitops_engine.overrides import (
    PENDING_ADD,
    Overrides,
    OverridesStore,
    override_errors,
)
from gitops_engine.schema import Union

from .deploy_plan import scope_live, scope_recs
from .identity import IDENTITIES_FILE, load_identities
from .linear_client import LiveCustomer
from .linear_files import partition_hidden
from .linear_overrides import OVERRIDES_FILE, OVERRIDES_FORMAT, base_value, load_overrides_file
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .writers import STATE_SUBDIR, load_desired, load_state, write_state


@dataclass(frozen=True)
class Settled:
    """An existing pending decision that is moot. `field` is a scalar field, or a list
    field's own name for pending adds; `values` are then the pending adds the registry
    now carries (the others stay pending), and empty for a scalar."""

    canonical_id: str
    field: str
    values: tuple[str, ...] = ()


@dataclass
class InboundResult:
    """What `sync-inbound` writes. `mirror` is the whole new mirror (out-of-scope rows
    kept on a scoped run); `settled` lists the pending decisions that are moot."""

    mirror: list[LinearCustomer]
    edits: list[Edit] = field(default_factory=list)
    deletions: Deletions = field(default_factory=Deletions)
    settled: list[Settled] = field(default_factory=list)


def _empty(value: Any) -> Any:
    return None if value in (None, "", []) else value


def _settled(
    overrides: Overrides,
    old_state: list[LinearCustomer],
    managed: list[LiveCustomer],
    desired: list[LinearCustomer],
) -> list[Settled]:
    """The pending decisions that are moot: a scalar whose live value now equals the
    desired value, and each pending list add the desired row now carries (compared by
    the field's key)."""
    pairs: dict[str, tuple[LinearCustomer, LiveCustomer]] = {}
    for p in pair_old_state(old_state, managed, desired, schema=LINEAR_SCHEMA):
        if p.live is not None and p.desired.external_ids:
            pairs.setdefault(p.desired.external_ids[0], (p.desired, p.live))
    keys = {f.name: f.policy.key for f in LINEAR_SCHEMA.fields if isinstance(f.policy, Union)}
    out: list[Settled] = []
    for cid, entry in overrides.entries.items():
        pair = pairs.get(cid)
        if pair is None:
            continue
        d, lv = pair
        for name, value in entry.pending.items():
            list_field = name.removesuffix(PENDING_ADD)
            if name.endswith(PENDING_ADD) and list_field in OVERRIDES_FORMAT.list_fields:
                key = keys.get(list_field, str)
                carried = {key(v) for v in LINEAR_SCHEMA.desired_value(d, list_field) or []}
                done = tuple(v for v in value if key(v) in carried)
                if done:
                    out.append(Settled(cid, list_field, done))
                continue
            live_value = base_value(name, LINEAR_SCHEMA.live_value(lv, name))
            desired_value = base_value(name, LINEAR_SCHEMA.desired_value(d, name))
            if _empty(live_value) == _empty(desired_value):
                out.append(Settled(cid, name))
    return sorted(out, key=lambda s: (s.canonical_id, s.field))


def read_inbound(
    data_dir: Path, dest: Destination[LiveCustomer], *, scope: str | None = None
) -> InboundResult:
    """The new mirror of `dest`'s live customers and the Linear edits to record.

    Reads the old mirror, `linear/`, `identities.yaml` and the overrides file; writes
    nothing. Raises `ValueError` when the overrides file or the identity table cannot
    be read; a `DestinationError` from the listing or a confirming `get` propagates."""
    data_dir = Path(data_dir)
    errors = override_errors(data_dir / OVERRIDES_FILE, OVERRIDES_FORMAT, LINEAR_SCHEMA)
    if errors:
        raise ValueError("\n".join(errors))
    overrides = load_overrides_file(data_dir)
    table = load_identities(data_dir / IDENTITIES_FILE)
    existing = load_state(data_dir)
    listing = dest.list_live()
    live = scope_live(listing, scope)
    mirror = reconcile_mirror(live, LINEAR_SCHEMA)
    if scope:
        # Scoped run: replace only the in-scope rows; out-of-scope state is untouched.
        mirror = [r for r in existing if not r.name.startswith(scope)] + mirror
    managed, _ = partition_hidden(live, hidden_from(overrides), table, scope=scope)
    old_state = scope_recs(existing, scope)
    desired = load_desired(data_dir)
    edits = detect_edits(old_state, managed, desired, schema=LINEAR_SCHEMA, base=base_value)
    settled = _settled(overrides, old_state, managed, desired)
    # The full listing, not the scoped one: a customer renamed out of scope is not gone.
    deletions = confirm_deletions(old_state, listing, desired, dest, schema=LINEAR_SCHEMA)
    return InboundResult(mirror, edits, deletions, settled)


def write_inbound(data_dir: Path, result: InboundResult, *, today: str) -> int:
    """Write `linear-state/` and its manifest, then drop the settled pending decisions
    from the overrides file, record the edits, and mark each confirmed deletion
    `deleted` as of `today` (an ISO date). Settling goes first, so a pending add
    recorded in this run is never dropped with an older one. Returns the number of
    override changes."""
    data_dir = Path(data_dir)
    store = OverridesStore(data_dir / OVERRIDES_FILE, OVERRIDES_FORMAT)
    write_state(result.mirror, data_dir)
    write_manifest(data_dir / STATE_SUBDIR, "sync-inbound")
    changed = 0
    for s in result.settled:
        if s.values:
            changed += store.drop_pending_values(s.canonical_id, s.field, s.values)
        else:
            changed += store.drop_pending(s.canonical_id, s.field)
    changed += record_edits(store, result.edits)
    for cid, live_id in result.deletions.confirmed:
        changed += store.mark_deleted(cid, live_id, today)
    store.save()
    return changed
