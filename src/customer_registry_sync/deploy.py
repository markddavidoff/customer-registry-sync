"""Deploy and restore for Linear customers: the engine's `plan_deploy`,
`apply_deploy` and `restore` with the customer schema, identity spec, ignore file and
`LinearDestination` bound.

The engine plans by identity and field policy, applies under check-and-set with a
pre-apply snapshot, and verifies each written row (`gitops_engine.deploy`,
`gitops_engine.snapshot`).
"""

from __future__ import annotations

from pathlib import Path

from gitops_engine import deploy as engine
from gitops_engine import snapshot as engine_snapshot
from gitops_engine.deploy import ApplyResult, DeployPlan
from gitops_engine.ignore import IgnoreList
from gitops_engine.retire import RetireEntry
from gitops_engine.snapshot import RestoreResult

from .identity import IDENTITY_SPEC, IdentityTable, implicit_identity
from .linear_client import LiveCustomer
from .linear_destination import LinearDestination
from .linear_files import IGNORE_FILE
from .linear_schema import LINEAR_SCHEMA, LinearCustomer

__all__ = ["ApplyResult", "DeployPlan", "RestoreResult", "apply_deploy", "plan_deploy", "restore"]


def plan_deploy(
    desired: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
    *,
    state: list[LinearCustomer] | None = None,
    retire: dict[str, RetireEntry] | None = None,
    other_live: list[LiveCustomer] | None = None,
) -> DeployPlan[LinearCustomer, LiveCustomer]:
    """Desired rows against live Linear customers (`gitops_engine.deploy.plan_deploy`).
    `state` is the `linear-state/` mirror; `retire` is keyed by first external id."""
    return engine.plan_deploy(
        desired,
        live,
        table,
        schema=LINEAR_SCHEMA,
        spec=IDENTITY_SPEC,
        implicit=implicit_identity,
        state=state,
        retire=retire,
        other_live=other_live,
    )


def apply_deploy(
    plan: DeployPlan[LinearCustomer, LiveCustomer],
    client,
    *,
    snapshot_dir: Path,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
) -> ApplyResult[LinearCustomer]:
    """Execute the plan's creates and updates against Linear
    (`gitops_engine.deploy.apply_deploy`). `ignore` defaults to the built-in prefix."""
    return engine.apply_deploy(
        plan,
        LinearDestination(client),
        schema=LINEAR_SCHEMA,
        snapshot_dir=snapshot_dir,
        ignore=ignore if ignore is not None else IgnoreList(IGNORE_FILE),
        scope=scope,
    )


def restore(path: Path, client, *, rows: list[str] | None = None) -> RestoreResult:
    """Write a snapshot back to Linear, skipping rows edited since
    (`gitops_engine.snapshot.restore`). `rows` filters by external id."""
    return engine_snapshot.restore(path, LinearDestination(client), schema=LINEAR_SCHEMA, rows=rows)
