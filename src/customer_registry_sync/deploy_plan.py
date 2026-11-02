"""The planning path `deploy` and `check` share, and `offline_plan`, which runs it with
the state mirror standing in for live Linear.

`load_inputs` reads what both commands plan from (the desired rows, the state
mirror, `identities.yaml`, `linear-ignore.yaml` and `linear-overrides.yaml`), and
refuses a data dir that still holds a legacy file or an invalid overrides file.
`plan_for_deploy` is `deploy`'s plan: one partition of live, the ignore conflicts, then
`plan_deploy`, with the rows a pending decision holds aborted. `check_for` is `check`'s
`run_check` call, and `check_exit` its exit code, offline and override findings included.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine.check import CheckResult
from gitops_engine.ignore import IgnoreList, load_ignore, partition_live
from gitops_engine.overrides import OverrideFindings, Overrides, held_rows, override_errors

from .check import EXIT_DRIFT, offline_errors, override_findings, run_check
from .datasets import load_datasets, load_datasets_config
from .deploy import DeployPlan, plan_deploy
from .identity import IDENTITIES_FILE, IdentityTable, load_identities
from .linear_client import LiveCustomer
from .linear_files import IGNORE_FILE, IGNORE_FORMAT, RETIRE_FILE, ignore_conflicts
from .linear_overrides import OVERRIDES_FILE, OVERRIDES_FORMAT, load_overrides_file
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .writers import DESIRED_SUBDIR, load_desired, load_state

# The legacy files (`linear_files.LEGACY_FILES`) whose presence refuses `check` and
# `deploy`. `linear-ignore.yaml` is still read until hidden overrides replace it.
LEGACY_GUARDED: tuple[str, ...] = (RETIRE_FILE,)

__all__ = [
    "LEGACY_GUARDED",
    "DeployPlanning",
    "PlanInputs",
    "check_exit",
    "check_for",
    "legacy_refused",
    "load_inputs",
    "load_state",
    "offline_plan",
    "plan_for_deploy",
    "state_to_live",
]


def state_to_live(row: LinearCustomer) -> LiveCustomer:
    """A state-mirror row as the live customer it records."""
    return LINEAR_SCHEMA.state_to_live(row)


def scope_live(live: list[LiveCustomer], scope: str | None) -> list[LiveCustomer]:
    return [c for c in live if c.name.startswith(scope)] if scope else live


def scope_recs(recs: list[LinearCustomer], scope: str | None) -> list[LinearCustomer]:
    return [r for r in recs if r.name.startswith(scope)] if scope else recs


def load_table(data_dir: Path) -> IdentityTable | None:
    """`<data_dir>/identities.yaml` (missing = empty); None after printing why it is invalid."""
    try:
        return load_identities(data_dir / IDENTITIES_FILE)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None


def load_ignore_file(data_dir: Path) -> IgnoreList | None:
    """`<data_dir>/linear-ignore.yaml` (missing = built-in prefix only); None after
    printing why it cannot be read."""
    try:
        return load_ignore(data_dir / IGNORE_FILE, IGNORE_FORMAT)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None


def entries(data_dir: Path):
    return load_datasets(data_dir, load_datasets_config(data_dir))


def partitioned(all_live: list[LiveCustomer], ignore: IgnoreList, scope: str | None):
    """The one partition: (managed, ignored, other). `managed` is the in-scope,
    non-ignored set the planner writes; `other` is every other live customer
    (ignored or out of scope), whose domains still count as held."""
    managed, ignored = partition_live(
        scope_live(all_live, scope), ignore, LINEAR_SCHEMA, scope=scope
    )
    managed_ids = {lv.id for lv in managed}
    other = [lv for lv in all_live if lv.id not in managed_ids]
    return managed, ignored, other


@dataclass
class PlanInputs:
    data_dir: Path
    desired: list[LinearCustomer]
    state: list[LinearCustomer]
    table: IdentityTable
    ignore: IgnoreList
    overrides: Overrides


def legacy_refused(data_dir: Path) -> bool:
    """True, after printing why, when the data dir still holds a legacy file this
    version no longer reads: running on would silently drop its decisions."""
    present = [name for name in LEGACY_GUARDED if (Path(data_dir) / name).exists()]
    for name in present:
        print(f"ERROR: {name} is no longer read by v0.3.0: run adopt-live", file=sys.stderr)
    return bool(present)


def load_overrides_checked(data_dir: Path) -> Overrides | None:
    """`linear-overrides.yaml` (missing = no entries); None after printing every error
    when the file is invalid. A deploy cannot plan without its hidden and deleted
    entries."""
    errors = override_errors(Path(data_dir) / OVERRIDES_FILE, OVERRIDES_FORMAT, LINEAR_SCHEMA)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if errors:
        return None
    return load_overrides_file(data_dir)


def load_inputs(
    data_dir: Path, scope: str | None, *, require_desired: bool = False
) -> PlanInputs | None:
    """The desired rows and state mirror (in scope), the identity table, the ignore
    list and the overrides; None after printing why one cannot be read, or when a
    legacy file is still present. With `require_desired`, an unscoped run with no
    desired rows is refused too (`generate` has not run)."""
    if legacy_refused(data_dir):
        return None
    overrides = load_overrides_checked(data_dir)
    if overrides is None:
        return None
    desired = scope_recs(load_desired(data_dir), scope)
    if require_desired and not desired and not scope:
        print(f"No {data_dir}/{DESIRED_SUBDIR}/* found — run `generate` first.", file=sys.stderr)
        return None
    state = scope_recs(load_state(data_dir), scope)
    table = load_table(data_dir)
    ignore = load_ignore_file(data_dir)
    if table is None or ignore is None:
        return None
    return PlanInputs(data_dir, desired, state, table, ignore, overrides)


@dataclass
class DeployPlanning:
    plan: DeployPlan[LinearCustomer, LiveCustomer]
    conflicts: list[str] = field(default_factory=list)  # ignore conflicts: refuse
    reports: list[str] = field(default_factory=list)  # stubs of ignored customers: note


def plan_for_deploy(
    inp: PlanInputs,
    all_live: list[LiveCustomer],
    scope: str | None,
) -> DeployPlanning:
    """`deploy`'s plan against `all_live`, every live customer Linear lists. A row with
    a pending decision in the overrides file is aborted, not written."""
    managed, ignored, other = partitioned(all_live, inp.ignore, scope)
    conflicts, reports = ignore_conflicts(
        ignored, inp.ignore, entries(inp.data_dir), inp.desired, inp.table, scope=scope
    )
    plan = plan_deploy(
        inp.desired,
        managed,
        inp.table,
        state=inp.state,
        other_live=other,
        held=held_rows(inp.overrides),
    )
    return DeployPlanning(plan, conflicts, reports)


def check_for(inp: PlanInputs, all_live: list[LiveCustomer], scope: str | None) -> CheckResult:
    """`check`'s `run_check` against `all_live`, every live customer Linear lists."""
    live = scope_live(all_live, scope)
    in_scope_ids = {lv.id for lv in live}
    return run_check(
        inp.desired,
        inp.state,
        live,
        inp.table,
        ignore=inp.ignore,
        scope=scope,
        entries=entries(inp.data_dir),
        other_live=[lv for lv in all_live if lv.id not in in_scope_ids],
        held=held_rows(inp.overrides),
    )


def check_exit(res: CheckResult, offline: list[str], findings: OverrideFindings) -> int:
    """`check`'s exit: the live checks' code, else EXIT_DRIFT on any offline error, any
    override failure or any pending decision."""
    return res.code or (EXIT_DRIFT if offline or findings.failures or findings.pending else 0)


def offline_plan(
    data_dir: Path,
    *,
    scope: str | None = None,
    live: list[LiveCustomer] | None = None,
) -> tuple[DeployPlan[LinearCustomer, LiveCustomer], int]:
    """`deploy`'s plan and `check`'s exit code, with no Linear access: `live` (default:
    every state-mirror row as the customer it records) stands in for Linear.

    Raises ValueError when the identity table, ignore file or overrides file cannot be
    read, or a legacy file is present."""
    data_dir = Path(data_dir)
    inp = load_inputs(data_dir, scope)
    if inp is None:
        raise ValueError(f"cannot plan {data_dir}: see the error above")
    if live is None:
        live = [state_to_live(r) for r in load_state(data_dir) if r.linear_synced is not None]
    planning = plan_for_deploy(inp, live, scope)
    res = check_for(inp, live, scope)
    return planning.plan, check_exit(res, offline_errors(data_dir), override_findings(data_dir))
