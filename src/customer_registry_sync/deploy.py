"""Deploy and restore for Linear customers: the engine's `plan_deploy`,
`apply_deploy` and `restore` with the customer schema, identity spec, hidden rows and
`LinearDestination` bound.

The engine plans by identity and field policy, applies under check-and-set with a
pre-apply snapshot, and verifies each written row (`gitops_engine.deploy`,
`gitops_engine.snapshot`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from gitops_engine import deploy as engine
from gitops_engine import snapshot as engine_snapshot
from gitops_engine.deploy import ApplyResult, DeployPlan
from gitops_engine.snapshot import RestoreResult

from .identity import IDENTITY_SPEC, IdentityTable, implicit_identity
from .linear_client import LiveCustomer
from .linear_destination import LinearDestination
from .linear_files import hidden_reason, no_hidden
from .linear_schema import LINEAR_SCHEMA, LinearCustomer

__all__ = ["ApplyResult", "DeployPlan", "RestoreResult", "apply_deploy", "plan_deploy", "restore"]


def plan_deploy(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
    *,
    state: list[LinearCustomer] | None = None,
    other_live: list[LiveCustomer] | None = None,
    held: Mapping[str, str] | None = None,
) -> DeployPlan[LinearCustomer, LiveCustomer]:
    """Desired rows against live Linear customers (`gitops_engine.deploy.plan_deploy`).
    `state` is the `linear-state/` mirror; `held` maps a canonical id to why its row is
    not written (`gitops_engine.overrides.held_rows`: a pending decision)."""
    return engine.plan_deploy(
        desired,
        live,
        table,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        implicit=implicit_identity,
        state=state,
        other_live=other_live,
        held=held,
    )


def apply_deploy(
    plan: DeployPlan[LinearCustomer, LiveCustomer],
    client,
    *,
    snapshot_dir: Path,
    exclude: Callable[[LiveCustomer], str | None] | None = None,
    scope: str | None = None,
) -> ApplyResult[LinearCustomer]:
    """Execute the plan's creates and updates against Linear
    (`gitops_engine.deploy.apply_deploy`). An update whose re-read row `exclude` gives
    a reason for is aborted (`linear_files.hidden_reason`); the default hides only the
    built-in prefix, under `scope`'s exemption."""
    if exclude is None:
        exclude = hidden_reason(no_hidden(), IdentityTable(), scope=scope)
    return engine.apply_deploy(
        plan,
        LinearDestination(client),
        schema=LINEAR_SCHEMA,
        snapshot_dir=snapshot_dir,
        scope=scope,
        exclude=exclude,
    )


def restore(path: Path, client, *, rows: list[str] | None = None) -> RestoreResult:
    """Write a snapshot back to Linear, skipping rows edited since
    (`gitops_engine.snapshot.restore`). `rows` filters by external id."""
    return engine_snapshot.restore(path, LinearDestination(client), schema=LINEAR_SCHEMA, rows=rows)
