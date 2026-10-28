"""Deploy: apply the desired Layer-2 state to Linear under per-customer
optimistic concurrency (the write gate).

`plan_deploy` pairs desired rows with live customers by identity and diffs them
field by field (`gitops_engine.fieldplan.plan_row`) into creates / updates / aborts / orphans
/ unchanged. `apply_deploy` performs create/update ONLY: it snapshots the live
rows first, re-reads each customer at write time and aborts on drift
(check-and-set), never a silent clobber; it sends only the planned fields and
verifies each written row. Orphans (live rows with no desired match) are
reported, never deleted.

Planning and apply are local code, not `gitops_engine.make_plan`/`apply`: the
engine's v0.1.0 `Adapter` contract has no seam for field-level changes,
many-to-one pairing, the snapshot, or post-write verification (see the
gitops-engine roadmap). `linear_adapter.LinearCustomersAdapter` still serves
the engine's drift detection and exit codes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine.fieldplan import (
    DESTRUCTIVE,
    FieldChange,
    plan_row,
    unacknowledged_destructive,
)
from gitops_engine.ignore import IgnoreList
from gitops_engine.retire import RetireEntry

from .identity import IdentityTable, pair_rows
from .linear_client import LinearError, LiveCustomer
from .linear_files import IGNORE_FILE
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .snapshot import record_written, write_snapshot


@dataclass
class DeployPlan:
    creates: list[LinearCustomer] = field(default_factory=list)
    updates: list[tuple[LinearCustomer, LiveCustomer]] = field(default_factory=list)
    aborts: list[tuple[LinearCustomer, LiveCustomer, str]] = field(default_factory=list)
    orphans: list[LiveCustomer] = field(default_factory=list)
    unchanged: list[LinearCustomer] = field(default_factory=list)
    # per matched (non-aborted) live id: the field changes plan_row computed
    changes: dict[str, list[FieldChange]] = field(default_factory=dict)
    # one message per destructive change not backed by a retire entry
    unacked: list[str] = field(default_factory=list)
    # planner failures: `check` exits 3 and `deploy` refuses on any
    errors: list[str] = field(default_factory=list)
    # (canonical id, live customer) for each extra live customer on one identity: never written
    known_duplicates: list[tuple[str, LiveCustomer]] = field(default_factory=list)
    # one message per desired domain not written because another live customer holds it
    withheld: list[str] = field(default_factory=list)
    # report-only lines (a stub with no live customer)
    notes: list[str] = field(default_factory=list)


def _withhold_shared(
    d: LinearCustomer,
    lv: LiveCustomer | None,
    holders: list[LiveCustomer],
    plan: DeployPlan,
) -> LinearCustomer:
    """`d` without the domains another live customer holds (shared domains are
    unverified). A domain the paired customer already holds is kept:
    writing it changes nothing."""
    own = {x.lower() for x in lv.domains} if lv is not None else set()
    keep: list[str] = []
    for dom in d.domains:
        key = dom.lower()
        other = (
            None
            if key in own
            else next(
                (
                    h
                    for h in holders
                    if (lv is None or h.id != lv.id) and key in {x.lower() for x in h.domains}
                ),
                None,
            )
        )
        if other is None:
            keep.append(dom)
            continue
        plan.withheld.append(
            f"withheld domain '{dom}' for '{d.name}': Linear customer "
            f"'{other.name}' already holds it (shared domains are unverified)"
        )
    return d if keep == d.domains else dataclasses.replace(d, domains=keep)


def plan_deploy(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
    *,
    state: list[LinearCustomer] | None = None,
    retire: dict[str, RetireEntry] | None = None,
    other_live: list[LiveCustomer] | None = None,
) -> DeployPlan:
    """Diff desired vs live, paired by identity (`pair_rows`). The
    optimistic-concurrency pre-state comes from the STATE mirror
    (`<data_dir>/linear-state/`), matched to the desired row by external_id/slug,
    falling back to the live row's linear_id. A row whose live updatedAt moved
    but whose deployable content is unchanged (a metadata-only edit) is NOT
    drift and proceeds normally.

    Each paired row goes through `plan_row` (field ownership rules); `updates`
    holds `(target, live)` where target is the effective row to write. `retire`
    is keyed by the desired row's first external id. `other_live` are live
    customers the plan never writes but whose domains still count as held."""
    plan = DeployPlan()
    state_by_xid: dict[str, LinearCustomer] = {}
    state_by_id: dict[str, LinearCustomer] = {}
    for s in state or []:
        for xid in s.external_ids:
            state_by_xid.setdefault(xid, s)
        if s.linear_synced is not None:
            state_by_id.setdefault(s.linear_synced.linear_id, s)

    pairing = pair_rows(desired, live, table or IdentityTable())
    plan.errors = list(pairing.errors)
    plan.known_duplicates = list(pairing.duplicates)
    plan.orphans = list(pairing.unmatched)
    holders = [*live, *(other_live or [])]
    for d, lv in pairing.pairs:
        if lv is None:
            if d.linear_owned:
                plan.notes.append(f"stub '{d.name}' has no Linear customer: run the refresh")
                continue
            plan.creates.append(_withhold_shared(d, None, holders, plan))
            continue
        st = next(
            (state_by_xid[x] for x in d.external_ids if x in state_by_xid), None
        ) or state_by_id.get(lv.id)
        ls = st.linear_synced if st is not None else None
        live_hash = LINEAR_SCHEMA.live_hash(lv)
        # a stub is Linear-owned: a Linear edit to it is not drift
        if (
            ls is not None
            and not d.linear_owned
            and lv.updated_at != ls.updated_at
            and live_hash != ls.content_hash
        ):
            plan.aborts.append(
                (
                    d,
                    lv,
                    f"drift: live updatedAt {lv.updated_at} != pre-state {ls.updated_at} (content changed)",
                )
            )
            continue
        if not d.linear_owned:
            d = _withhold_shared(d, lv, holders, plan)
        entry = (retire or {}).get(d.external_ids[0]) if d.external_ids else None
        target, changes = plan_row(d, lv, entry, schema=LINEAR_SCHEMA)
        plan.changes[lv.id] = changes
        plan.unacked += [
            f"{d.name}: {c.field} {c.kind} without retire"
            for c in unacknowledged_destructive(changes)
        ]
        if not changes:
            plan.unchanged.append(target)
            continue
        plan.updates.append((target, lv))
    return plan


def _persisted(lv: LiveCustomer) -> LinearCustomer:
    """The state-mirror row for a row we just wrote: built from what Linear
    RETURNED (not from desired), so its content_hash agrees with what
    `check`/`plan_deploy`/`sync-inbound` compute from live."""
    return LINEAR_SCHEMA.mirror_row(lv)


def _non_empty(**fields):
    """Drop None / [] values: creates never send empties."""
    return {k: v for k, v in fields.items() if v is not None and v != []}


@dataclass
class ApplyResult:
    persisted: list[LinearCustomer]
    snapshot: Path
    created: list[str]
    failure: str | None = None


def _update_fields(
    target: LinearCustomer,
    changes: list[FieldChange],
    status_ids: dict[str, str],
    tier_ids: dict[str, str],
) -> dict:
    """The update kwargs for exactly the fields `changes` names. `domains` sends
    the full target list; a cleared tier/revenue sends None."""
    send: dict = {}
    for c in changes:
        if c.field == "name":
            send["name"] = target.name
        elif c.field == "status":
            send["status_id"] = status_ids[target.status]
        elif c.field == "external_ids":
            send["external_ids"] = list(target.external_ids)
        elif c.field == "domains":
            send["domains"] = list(target.domains)
        elif c.field == "revenue":
            send["revenue"] = target.revenue
        elif c.field == "tier":
            send["tier_id"] = tier_ids[target.tier] if target.tier else None
        else:
            raise ValueError(f"unknown planned field {c.field!r}")
    return send


def _mismatch(
    target: LinearCustomer,
    got: LiveCustomer,
    before: LiveCustomer,
    changes: list[FieldChange],
) -> str | None:
    """First managed field where the row Linear returned differs from the target.

    Status is the one field a target cannot mirror exactly: a stub whose
    live status is empty gets a placeholder (a row needs a status). When no
    status write was planned, the expected status is the one before the write."""
    status = target.status
    if not any(c.field == "status" for c in changes):
        status = before.status
    checks = [
        ("name", target.name, got.name, target.name == got.name),
        ("status", status, got.status, status == got.status),
        (
            "external_ids",
            target.external_ids,
            got.external_ids,
            sorted(target.external_ids) == sorted(got.external_ids),
        ),
        (
            "domains",
            target.domains,
            got.domains,
            sorted(d.lower() for d in target.domains) == sorted(d.lower() for d in got.domains),
        ),
        ("revenue", target.revenue, got.revenue, target.revenue == got.revenue),
        ("tier", target.tier, got.tier, target.tier == got.tier),
    ]
    for name, want, have, ok in checks:
        if not ok:
            return f"{target.name}: {name} expected {want} got {have}"
    return None


def apply_deploy(
    plan: DeployPlan,
    client,
    *,
    snapshot_dir: Path,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
) -> ApplyResult:
    """Execute creates/updates against Linear.

    Order: status/tier pre-flight -> snapshot of every live row in
    `plan.updates` (an OSError propagates before any write) -> creates -> per
    update: check-and-set re-read, `update_customer` with ONLY the planned
    fields (`retire=True` when a planned change is destructive), record the
    written `updatedAt` in the snapshot, then verify the returned row equals the
    target on every managed field. A mismatch or a Linear error sets `failure`
    and stops: later rows are recorded in `plan.aborts` as not attempted.
    Never touches orphans. A row whose write-time re-read matches `ignore`
    (default: the built-in prefix) is aborted, never written."""
    if plan.errors:
        raise ValueError(f"planner error(s), nothing written: {plan.errors}")
    if plan.unacked or any(unacknowledged_destructive(cs) for cs in plan.changes.values()):
        raise ValueError(f"unacknowledged destructive change(s), nothing written: {plan.unacked}")
    status_ids = client.status_ids()
    tier_ids = client.tier_ids()

    # Pre-flight: every status and tier the plan writes must resolve to an id
    # BEFORE any write — otherwise an unresolvable name mid-loop would raise
    # after earlier creates already hit Linear (lossy, and a re-run would
    # duplicate them).
    needed = {d.status for d in plan.creates} | {
        d.status
        for d, lv in plan.updates
        if any(c.field == "status" for c in plan.changes.get(lv.id, []))
    }
    missing = sorted(needed - set(status_ids))
    if missing:
        raise ValueError(f"unresolvable Linear status(es), nothing written: {missing}")
    tiers = {d.tier for d in plan.creates if d.tier} | {
        d.tier
        for d, lv in plan.updates
        if d.tier and any(c.field == "tier" for c in plan.changes.get(lv.id, []))
    }
    missing = sorted(tiers - set(tier_ids))
    if missing:
        raise ValueError(f"unresolvable Linear tier(s), nothing written: {missing}")

    ignore = ignore if ignore is not None else IgnoreList(IGNORE_FILE)
    snapshot = write_snapshot([lv for _, lv in plan.updates], snapshot_dir)
    result = ApplyResult(persisted=[], snapshot=snapshot, created=[])

    for d in plan.creates:
        try:
            lv = client.create_customer(
                **_non_empty(
                    name=d.name,
                    domains=d.domains,
                    external_ids=d.external_ids,
                    status_id=status_ids[d.status],
                    tier_id=tier_ids[d.tier] if d.tier else None,
                    revenue=d.revenue,
                )
            )
        except LinearError as e:
            result.failure = f"{d.name}: create failed: {e}"
            break
        result.created.append(lv.id)
        result.persisted.append(_persisted(lv))

    for i, (d, lv0) in enumerate(plan.updates):
        if result.failure is not None:
            plan.aborts += [
                (t, lv, "not attempted: apply stopped at an earlier failure")
                for t, lv in plan.updates[i:]
            ]
            break
        changes = plan.changes.get(lv0.id, [])
        if not changes:
            continue
        cur = client.get_customer(lv0.id)  # write-time re-read (check-and-set)
        if cur is None:
            plan.aborts.append((d, lv0, "gone at write time"))
            continue
        ignored = ignore.match(cur.id, cur.name, scope=scope)
        if ignored is not None:
            plan.aborts.append((d, lv0, f"ignored at write time: {ignored.reason}"))
            continue
        # Guard EVERY update — including first-link rows (linear_synced is None):
        # compare the current state to what we observed at plan time. If the row
        # changed in between, abort rather than clobber a concurrent edit.
        if cur.updated_at != lv0.updated_at:
            plan.aborts.append((d, lv0, f"write-time drift {cur.updated_at} != {lv0.updated_at}"))
            continue
        retire = any(c.kind in DESTRUCTIVE for c in changes)
        try:
            lv = client.update_customer(
                cur.id, retire=retire, **_update_fields(d, changes, status_ids, tier_ids)
            )
        except LinearError as e:
            result.failure = f"{d.name}: update failed: {e}"
            continue
        result.persisted.append(_persisted(lv))
        try:
            record_written(snapshot, lv.id, lv.updated_at)
        except OSError as e:
            result.failure = f"{d.name}: written, but recording it in {snapshot} failed: {e}"
            continue
        result.failure = _mismatch(d, lv, cur, changes)

    return result
