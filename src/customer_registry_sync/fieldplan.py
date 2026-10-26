"""Field-level planner: per-field ownership rules for one paired customer.

`plan_row` turns a desired row and its live pair into the effective target
row plus a list of `FieldChange`s:

- A Linear-owned stub row (``linear_owned``) takes every value from live; only
  its canonical ``external_ids`` is written.
- ``name``, ``status``, ``external_ids`` are registry-owned and always written.
- ``domains`` is the registry value unioned with live, minus retired entries.
  A live-only domain is never dropped unless a retire entry names it.
- ``revenue``, ``tier`` are registry-owned when the registry has a value; when
  it has none, the live value is kept. A retire entry whose value equals live
  clears it.

`remove` and `clear` are destructive. The planner emits them only from a
retire entry and marks them ``retire=True``; `unacknowledged_destructive`
finds any that are not, which the callers treat as a planner bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .curated import CuratedFields
from .identity import IdentityTable
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer

ChangeKind = Literal["set", "change", "add", "remove", "clear"]
DESTRUCTIVE: frozenset = frozenset({"remove", "clear"})


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any
    kind: ChangeKind
    retire: bool = False


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _plain(field_name: str, old: Any, new: Any) -> list[FieldChange]:
    """A registry-owned scalar: written when it differs from live."""
    if old == new:
        return []
    return [FieldChange(field_name, old, new, "set" if _empty(old) else "change")]


def _domains(
    desired: list[str], live: list[str], retired: list[str]
) -> tuple[list[str], list[FieldChange]]:
    live_by_key = {d.lower(): d for d in live}
    retired_keys = {d.lower() for d in retired}
    target: list[str] = []
    seen: set[str] = set()
    changes: list[FieldChange] = []
    for dom in desired:
        key = dom.lower()
        if key in seen or key in retired_keys:
            continue
        seen.add(key)
        if key in live_by_key:
            target.append(live_by_key[key])  # keep live casing: not a change
        else:
            target.append(dom)
            changes.append(FieldChange("domains", None, dom, "add"))
    for dom in live:
        key = dom.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in retired_keys:
            changes.append(FieldChange("domains", dom, None, "remove", retire=True))
        else:
            target.append(dom)
    return target, changes


def _owned_when_set(
    field_name: str, desired: Any, live: Any, retired: Any
) -> tuple[Any, list[FieldChange]]:
    """revenue / tier: retire (exact match) clears; else desired wins; else keep live."""
    if retired is not None and retired == live:
        return None, [FieldChange(field_name, live, None, "clear", retire=True)]
    if desired is None:
        return live, []
    return desired, _plain(field_name, live, desired)


def _plan_linear_owned(
    d: LinearCustomer, lv: LiveCustomer
) -> tuple[LinearCustomer, list[FieldChange]]:
    """A stub row: Linear owns every field but the canonical id, so
    the target is the live row with the desired external id, and the only
    possible change is that id. Retire entries never apply."""
    changes: list[FieldChange] = []
    if sorted(lv.external_ids) != sorted(d.external_ids):
        changes.append(
            FieldChange("external_ids", list(lv.external_ids), list(d.external_ids), "change")
        )
    target = LinearCustomer(
        name=lv.name,
        domains=list(lv.domains),
        revenue=lv.revenue,
        tier=lv.tier,
        status=lv.status or d.status,
        external_ids=list(d.external_ids),
        linear_synced=d.linear_synced,
        linear_owned=True,
    )
    return target, changes


def plan_row(
    d: LinearCustomer, lv: LiveCustomer, retire: CuratedFields | None
) -> tuple[LinearCustomer, list[FieldChange]]:
    """The effective target row for `d` against its live match, and its changes."""
    if d.linear_owned:
        return _plan_linear_owned(d, lv)
    retire = retire or CuratedFields()
    changes: list[FieldChange] = []
    changes += _plain("name", lv.name, d.name)
    changes += _plain("status", lv.status, d.status)
    if sorted(lv.external_ids) != sorted(d.external_ids):
        changes.append(
            FieldChange("external_ids", list(lv.external_ids), list(d.external_ids), "change")
        )
    domains, dom_changes = _domains(d.domains, lv.domains, retire.domains)
    changes += dom_changes
    revenue, rev_changes = _owned_when_set("revenue", d.revenue, lv.revenue, retire.revenue)
    changes += rev_changes
    tier, tier_changes = _owned_when_set("tier", d.tier, lv.tier, retire.tier)
    changes += tier_changes
    target = LinearCustomer(
        name=d.name,
        domains=domains,
        revenue=revenue,
        tier=tier,
        status=d.status,
        external_ids=list(d.external_ids),
        linear_synced=d.linear_synced,
    )
    return target, changes


def unacknowledged_destructive(changes: list[FieldChange]) -> list[FieldChange]:
    """Destructive changes not backed by a retire entry (a planner bug)."""
    return [c for c in changes if c.kind in DESTRUCTIVE and not c.retire]


@dataclass(frozen=True)
class Untracked:
    """A live value the registry does not carry and no retire entry names."""

    external_id: str
    name: str
    field: str
    value: Any


def _xid(d: LinearCustomer) -> str:
    return d.external_ids[0] if d.external_ids else ""


def _paired(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None,
) -> list[tuple[LinearCustomer, LiveCustomer | None]]:
    from .deploy import pair_rows  # deploy imports this module: import lazily

    return pair_rows(desired, live, table or IdentityTable()).pairs


def untracked_values(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None,
    retire: dict[str, CuratedFields] | None,
) -> list[Untracked]:
    """Live values on paired rows that the registry does not carry.

    A live domain absent from the desired row (case-insensitive), or a live
    revenue/tier where the desired row has none, is untracked unless a retire
    entry names it. Live rows with no desired row (orphans) and Linear-owned
    stub rows (Linear is their source) are ignored.
    """
    out: list[Untracked] = []
    for d, lv in _paired(desired, live, table):
        if lv is None or d.linear_owned:
            continue
        xid = _xid(d)
        entry = (retire or {}).get(xid) or CuratedFields()
        have = {x.lower() for x in d.domains}
        gone = {x.lower() for x in entry.domains}
        seen: set[str] = set()
        for dom in lv.domains:
            key = dom.lower()
            if key in have or key in gone or key in seen:
                continue
            seen.add(key)
            out.append(Untracked(xid, d.name, "domains", dom))
        if d.revenue is None and lv.revenue is not None and entry.revenue != lv.revenue:
            out.append(Untracked(xid, d.name, "revenue", lv.revenue))
        if d.tier is None and lv.tier is not None and entry.tier != lv.tier:
            out.append(Untracked(xid, d.name, "tier", lv.tier))
    return out


def stale_retire(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None,
    retire: dict[str, CuratedFields] | None,
) -> list[str]:
    """One message per retire entry value that cannot do its job.

    An entry is stale when its external id has no desired row, the row is a
    Linear-owned stub (the planner never applies retire to one), the row has no
    live match, or the retired value is not live. It also conflicts when the
    registry still carries the value: it would be cleared, then set again on
    alternate runs. For revenue and tier the registry owns the field whenever
    it sets one (even a different value), so any non-None desired value
    conflicts.
    """
    if not retire:
        return []
    live_for = {id(d): lv for d, lv in _paired(desired, live, table)}
    by_xid = {_xid(d): d for d in desired if d.external_ids}
    msgs: list[str] = []
    for xid, entry in retire.items():
        d = by_xid.get(xid)
        if d is None:
            msgs.append(f"{xid}: no registry row")
            continue
        if d.linear_owned:
            msgs.append(f"{xid}: Linear-owned stub (retire does not apply)")
            continue
        lv = live_for.get(id(d))
        if lv is None:
            msgs.append(f"{xid}: no live match")
            continue
        live_doms = {x.lower() for x in lv.domains}
        want_doms = {x.lower() for x in d.domains}
        for dom in entry.domains:
            if dom.lower() not in live_doms:
                msgs.append(f"{xid}: domains {dom} not live")
            if dom.lower() in want_doms:
                msgs.append(f"{xid}: domains {dom} conflicts with registry")
        for field_name, retired, live_val, want_val in (
            ("revenue", entry.revenue, lv.revenue, d.revenue),
            ("tier", entry.tier, lv.tier, d.tier),
        ):
            if retired is None:
                continue
            if retired != live_val:
                msgs.append(f"{xid}: {field_name} {retired} not live")
            if want_val is not None:
                msgs.append(f"{xid}: {field_name} {retired} conflicts with registry")
    return msgs


def state_to_live(rec: LinearCustomer) -> LiveCustomer:
    """A `LiveCustomer` view of a state-mirror row (id/updatedAt from `linear_synced`)."""
    ls = rec.linear_synced
    return LiveCustomer(
        id=ls.linear_id if ls else "",
        name=rec.name,
        domains=list(rec.domains),
        revenue=rec.revenue,
        tier=rec.tier,
        status=rec.status or "",
        external_ids=list(rec.external_ids),
        updated_at=ls.updated_at if ls else "",
    )
