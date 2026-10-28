"""The gitops-engine `Adapter` for Linear Customers, for drift detection only.

`check` drives `gitops_engine.detect_drift` through this adapter and keys its
exit codes off the engine's `EXIT_*`. Drift detection needs only `live_id`,
`live_hash` and `state_mark`. Planning and writes do not go through the engine:
`deploy.plan_deploy` / `apply_deploy` are local code (field-level changes,
pairing by identity through `identity.pair_rows`, snapshot, post-write
verification), and `check` reports that same plan. So the adapter's matching,
mark lookup and write methods refuse: there is one pairing, `pair_rows`, and no
per-row second copy of it here to drift away from it.
"""

from __future__ import annotations

from gitops_engine import SyncMark

from .linear_client import LiveCustomer
from .linear_schema import LINEAR_SCHEMA, LinearCustomer, LinearSynced, content_hash_of

_PLANNING_REFUSED = "planning goes through deploy.plan_deploy (pairing: deploy.pair_rows)"
_WRITES_REFUSED = "writes go through deploy.apply_deploy, not the engine adapter"


def _mark_of(ls: LinearSynced) -> SyncMark:
    """A `LinearSynced` block as the engine's optimistic-concurrency mark.
    `updated_at` is the version token; `content_hash` is the recorded hash."""
    return SyncMark(ls.linear_id, ls.updated_at, ls.content_hash)


class LinearCustomersAdapter:
    """Drives `gitops_engine.detect_drift` against Linear Customers.

    The read methods drift detection uses are pure. `match` and `mark_for`
    (planning) and `preflight`, `get`, `create` and `update` (writes) raise:
    the local planner and apply in `deploy` own those.
    """

    # --- what drift detection reads ---
    def desired_hash(self, d: LinearCustomer) -> str:
        return content_hash_of(d)

    def live_hash(self, lv: LiveCustomer) -> str:
        return LINEAR_SCHEMA.live_hash(lv)

    def live_id(self, lv: LiveCustomer) -> str:
        return lv.id

    def live_version(self, lv: LiveCustomer) -> str:
        return lv.updated_at

    def state_mark(self, s: LinearCustomer) -> SyncMark | None:
        if s.linear_synced is None:
            return None  # untracked row (hand-edited/legacy) — not evidence either way
        return _mark_of(s.linear_synced)

    # --- planning (refused: see deploy.plan_deploy) ---
    def match(self, d: LinearCustomer, live: list[LiveCustomer]) -> LiveCustomer | None:
        raise NotImplementedError(_PLANNING_REFUSED)

    def mark_for(
        self, d: LinearCustomer, lv: LiveCustomer, state: list[LinearCustomer]
    ) -> SyncMark | None:
        raise NotImplementedError(_PLANNING_REFUSED)

    # --- writes (refused: see deploy.apply_deploy) ---
    def preflight(self, plan) -> None:
        raise NotImplementedError(_WRITES_REFUSED)

    def get(self, live_id: str) -> LiveCustomer | None:
        raise NotImplementedError(_WRITES_REFUSED)

    def create(self, d: LinearCustomer) -> LiveCustomer:
        raise NotImplementedError(_WRITES_REFUSED)

    def update(self, live_id: str, d: LinearCustomer) -> LiveCustomer:
        raise NotImplementedError(_WRITES_REFUSED)
