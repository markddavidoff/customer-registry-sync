"""The gitops-engine `Adapter` for Linear Customers.

This is the one place that knows how Linear Customers are matched, hashed,
read, and written. The engine (`gitops_engine`) drives the plan / drift /
check-and-set logic through this contract; the behavior is exactly what
`deploy.plan_deploy` / `apply_deploy` and `check.detect_drift` carried before.

Matching order (unchanged): external ids, then domains, then the normalized
name (status suffix stripped on both sides), then the curated alias. Writes
resolve Linear's status/tier ids; `preflight` fails the whole apply before any
write when a needed status does not resolve.
"""

from __future__ import annotations

import re

from gitops_engine import SyncMark

from .identity import strip_status_suffix
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer, LinearSynced, content_hash_of


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _match_live(
    d: LinearCustomer, live: list[LiveCustomer], aliases: dict[str, str] | None = None
) -> LiveCustomer | None:
    xids = set(d.external_ids)
    if xids:
        for lv in live:
            if xids & set(lv.external_ids):
                return lv
    doms = set(d.domains)
    if doms:
        for lv in live:
            if doms & set(lv.domains):
                return lv
    dn = _norm(strip_status_suffix(d.name))
    for lv in live:
        if _norm(strip_status_suffix(lv.name)) == dn:
            return lv
    # curated identity link: our dataset name -> an existing Linear customer name
    # aliases are keyed by the dataset canonical_name, i.e. without the status suffix
    alias_name = (aliases or {}).get(strip_status_suffix(d.name))
    if alias_name:
        for lv in live:
            if strip_status_suffix(lv.name) == alias_name:
                return lv
    return None


def live_to_linear(lv: LiveCustomer) -> LinearCustomer:
    return LinearCustomer(
        name=lv.name,
        domains=list(lv.domains),
        revenue=lv.revenue,
        tier=lv.tier,
        status=lv.status or "Active",
        external_ids=list(lv.external_ids),
    )


def _persisted(lv: LiveCustomer) -> LinearCustomer:
    """The state-mirror row for a row we just wrote: built from what Linear
    RETURNED (not from desired), so its content_hash agrees with what
    `check`/`plan_deploy`/`sync-inbound` compute from live."""
    rec = live_to_linear(lv)
    rec.linear_synced = LinearSynced(lv.id, lv.updated_at, content_hash_of(rec))
    return rec


def _mark_of(ls: LinearSynced) -> SyncMark:
    """A `LinearSynced` block as the engine's optimistic-concurrency mark.
    `updated_at` is the version token; `content_hash` is the recorded hash."""
    return SyncMark(ls.linear_id, ls.updated_at, ls.content_hash)


class LinearCustomersAdapter:
    """Drives `gitops_engine` against Linear Customers.

    Reads and identity are pure. Writes (`preflight`, `create`, `update`) go
    through `client` (a `LinearClient` or a test double exposing the same
    `status_ids`/`tier_ids`/`get_customer`/`create_customer`/`update_customer`).
    `client` may be None for read-only uses (`detect_drift`, planning), which
    never touch it.
    """

    def __init__(self, client, aliases: dict[str, str]) -> None:
        self._client = client
        self._aliases = aliases
        self._status_ids: dict[str, str] | None = None
        self._tier_ids: dict[str, str] | None = None

    # --- reads / identity ---
    def match(self, d: LinearCustomer, live: list[LiveCustomer]) -> LiveCustomer | None:
        return _match_live(d, live, self._aliases)

    def desired_hash(self, d: LinearCustomer) -> str:
        return content_hash_of(d)

    def live_hash(self, lv: LiveCustomer) -> str:
        return content_hash_of(live_to_linear(lv))

    def live_id(self, lv: LiveCustomer) -> str:
        return lv.id

    def live_version(self, lv: LiveCustomer) -> str:
        return lv.updated_at

    def mark_for(
        self, d: LinearCustomer, lv: LiveCustomer, state: list[LinearCustomer]
    ) -> SyncMark | None:
        """Locate the state row for this desired/live pair and return its mark.

        The pre-state comes from the STATE mirror, matched to the desired row by
        external_id/slug, falling back to the live row's linear_id — the exact
        two-step lookup the former `plan_deploy` did."""
        state_by_xid: dict[str, LinearCustomer] = {}
        state_by_id: dict[str, LinearCustomer] = {}
        for s in state:
            for xid in s.external_ids:
                state_by_xid.setdefault(xid, s)
            if s.linear_synced is not None:
                state_by_id.setdefault(s.linear_synced.linear_id, s)
        st = next(
            (state_by_xid[x] for x in d.external_ids if x in state_by_xid), None
        ) or state_by_id.get(lv.id)
        if st is None or st.linear_synced is None:
            return None
        return _mark_of(st.linear_synced)

    def state_mark(self, s: LinearCustomer) -> SyncMark | None:
        if s.linear_synced is None:
            return None  # untracked row (hand-edited/legacy) — not evidence either way
        return _mark_of(s.linear_synced)

    # --- writes ---
    def _resolve_ids(self) -> tuple[dict[str, str], dict[str, str]]:
        # Fetched once (as the former apply_deploy did) and cached, so preflight
        # and every create/update share one read of the workspace statuses/tiers.
        if self._status_ids is None:
            self._status_ids = self._client.status_ids()
            self._tier_ids = self._client.tier_ids()
        assert self._tier_ids is not None
        return self._status_ids, self._tier_ids

    def preflight(self, plan) -> None:
        """Every status the plan needs must resolve to an id BEFORE any write —
        otherwise an unresolvable status mid-loop would raise after earlier
        creates already hit Linear (lossy, and a re-run would duplicate them)."""
        status_ids, _ = self._resolve_ids()
        needed = {d.status for d in plan.creates} | {d.status for d, _ in plan.updates}
        missing = sorted(needed - set(status_ids))
        if missing:
            raise ValueError(f"unresolvable Linear status(es), nothing written: {missing}")

    def get(self, live_id: str) -> LiveCustomer | None:
        return self._client.get_customer(live_id)

    def create(self, d: LinearCustomer) -> LiveCustomer:
        status_ids, tier_ids = self._resolve_ids()
        return self._client.create_customer(
            name=d.name,
            domains=d.domains,
            external_ids=d.external_ids,
            status_id=status_ids[d.status],
            tier_id=tier_ids.get(d.tier or ""),
            revenue=d.revenue,
        )

    def update(self, live_id: str, d: LinearCustomer) -> LiveCustomer:
        status_ids, tier_ids = self._resolve_ids()
        return self._client.update_customer(
            live_id,
            name=d.name,
            domains=d.domains,
            external_ids=d.external_ids,
            status_id=status_ids[d.status],
            tier_id=tier_ids.get(d.tier or ""),
            revenue=d.revenue,
        )
