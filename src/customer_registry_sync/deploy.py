"""Deploy: apply the desired Layer-2 state to Linear under
per-customer optimistic concurrency.

The plan/drift/apply decision logic now lives in `gitops_engine`; this module is
a thin Linear-specific wrapper over it. `plan_deploy` diffs desired vs live into
creates / updates / aborts / orphans / unchanged. `apply_deploy` performs
create/update ONLY, re-reading each customer at write time and aborting on drift
(check-and-set) — never a silent clobber. Orphans (live rows with no desired
match) are reported, never deleted. The `LinearCustomersAdapter` supplies
matching, hashing, identity, reads, and writes.
"""

from __future__ import annotations

from gitops_engine import Plan, apply, make_plan

from .linear_adapter import (
    LinearCustomersAdapter,
    _match_live,
    _persisted,
    live_to_linear,
)
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer

# `DeployPlan` is the engine's `Plan` specialized to Linear's record types. Kept
# as a name so existing callers/tests construct and field-access it unchanged.
DeployPlan = Plan[LinearCustomer, LiveCustomer]

__all__ = [
    "DeployPlan",
    "_match_live",
    "_persisted",
    "apply_deploy",
    "live_to_linear",
    "plan_deploy",
]


def plan_deploy(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    aliases: dict[str, str] | None = None,
    *,
    state: list[LinearCustomer] | None = None,
) -> DeployPlan:
    """Diff desired vs live. The optimistic-concurrency pre-state comes from the
    STATE mirror (customers/linear-state/), matched to the desired row by
    external_id/slug, falling back to the live row's linear_id. A row whose
    live updatedAt moved but whose deployable content is unchanged (a
    metadata-only edit) is NOT drift and proceeds normally."""
    adapter = LinearCustomersAdapter(client=None, aliases=aliases or {})
    return make_plan(desired, live, state or [], adapter)


def apply_deploy(plan: DeployPlan, client) -> list[LinearCustomer]:
    """Execute creates/updates against Linear. Returns the records to persist
    back to `customers/linear/*` with refreshed `linear_synced`, built from the
    rows Linear RETURNED. Mutates `plan.aborts` for any write-time drift. Does
    NOT touch orphans. Preflight (inside the engine) fails before any write when
    a needed status does not resolve."""
    adapter = LinearCustomersAdapter(client, aliases={})
    written = apply(plan, adapter)
    return [_persisted(lv) for lv in written]
