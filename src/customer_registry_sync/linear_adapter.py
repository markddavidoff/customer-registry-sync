"""The gitops-engine `Adapter` for Linear Customers, for drift detection only.

`check` drives `gitops_engine.detect_drift` / `run_check` through this adapter
and keys its exit codes off the engine's `EXIT_*`. Planning and writes no longer
go through the engine: `deploy.plan_deploy` / `apply_deploy` are local code
(field-level changes, pairing by identity, snapshot, post-write verification),
so this adapter's write methods refuse.

Matching goes through the identity table (`identity.resolve`): a desired row and
a live customer match when they resolve to the same identity. With several live
customers on one identity, `deploy.pick_primary` chooses; with no choice, there
is no match (`plan_deploy` reports that case as a planner error).
"""

from __future__ import annotations

from gitops_engine import SyncMark

from .deploy import live_to_linear, pick_primary
from .identity import IdentityTable, keys_for_live, keys_for_row, resolve, with_implicit
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer, LinearSynced, content_hash_of

_WRITES_REFUSED = "writes go through deploy.apply_deploy, not the engine adapter"


def _mark_of(ls: LinearSynced) -> SyncMark:
    """A `LinearSynced` block as the engine's optimistic-concurrency mark.
    `updated_at` is the version token; `content_hash` is the recorded hash."""
    return SyncMark(ls.linear_id, ls.updated_at, ls.content_hash)


class LinearCustomersAdapter:
    """Drives `gitops_engine` drift detection against Linear Customers.

    Reads and identity are pure. `get` reads through `client` (a `LinearClient`
    or a test double), which may be None for drift detection, which never
    touches it. `preflight`, `create` and `update` raise: writes go through
    `deploy.apply_deploy`.
    """

    def __init__(self, client=None, table: IdentityTable | None = None) -> None:
        self._client = client
        self._table = table or IdentityTable()

    # --- reads / identity ---
    def match(self, d: LinearCustomer, live: list[LiveCustomer]) -> LiveCustomer | None:
        """The live customer on `d`'s identity. A row that resolves to nothing gets
        an implicit identity, as `deploy.pair_rows` gives it."""
        table = with_implicit(self._table, [d])
        hit = resolve(keys_for_row(d), table)
        assert hit is not None  # with_implicit gives every row an identity
        group = [
            lv
            for lv in live
            if (h := resolve(keys_for_live(lv), table)) is not None
            and h.canonical_id == hit.canonical_id
        ]
        if len(group) <= 1:
            return group[0] if group else None
        return pick_primary(hit.canonical_id, group, table)

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

    # --- writes (refused: see deploy.apply_deploy) ---
    def preflight(self, plan) -> None:
        raise NotImplementedError(_WRITES_REFUSED)

    def get(self, live_id: str) -> LiveCustomer | None:
        return self._client.get_customer(live_id)

    def create(self, d: LinearCustomer) -> LiveCustomer:
        raise NotImplementedError(_WRITES_REFUSED)

    def update(self, live_id: str, d: LinearCustomer) -> LiveCustomer:
        raise NotImplementedError(_WRITES_REFUSED)
