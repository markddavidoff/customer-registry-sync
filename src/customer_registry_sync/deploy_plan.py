"""The planning path `deploy` and `check` share, and `offline_plan`, which runs it with
the state mirror standing in for live Linear.

`load_inputs` reads what both commands plan from (the desired rows, the state
mirror, `identities.yaml` and `linear-ignore.yaml`). `plan_for_deploy` is `deploy`'s
plan: one partition of live, the ignore conflicts, then `plan_deploy`.
`check_for` is `check`'s `run_check` call, and `check_exit` its exit code, offline
errors included.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from gitops_engine.check import CheckResult
from gitops_engine.ignore import IgnoreList, load_ignore, partition_live
from gitops_engine.retire import RetireEntry, load_retire

from .check import EXIT_DRIFT, offline_errors, run_check
from .datasets import load_datasets, load_datasets_config
from .deploy import DeployPlan, plan_deploy
from .identity import IDENTITIES_FILE, IdentityTable, load_identities
from .linear_client import LiveCustomer
from .linear_files import IGNORE_FILE, IGNORE_FORMAT, RETIRE_FILE, ignore_conflicts
from .linear_schema import LINEAR_SCHEMA, LinearCustomer
from .writers import DESIRED_SUBDIR, load_desired, load_state

__all__ = [
    "DeployPlanning",
    "PlanInputs",
    "check_exit",
    "check_for",
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


def load_inputs(
    data_dir: Path, scope: str | None, *, require_desired: bool = False
) -> PlanInputs | None:
    """The desired rows and state mirror (in scope), the identity table and the ignore
    list; None after printing why one cannot be read. With `require_desired`, an
    unscoped run with no desired rows is refused too (`generate` has not run)."""
    desired = scope_recs(load_desired(data_dir), scope)
    if require_desired and not desired and not scope:
        print(f"No {data_dir}/{DESIRED_SUBDIR}/* found — run `generate` first.", file=sys.stderr)
        return None
    state = scope_recs(load_state(data_dir), scope)
    table = load_table(data_dir)
    ignore = load_ignore_file(data_dir)
    if table is None or ignore is None:
        return None
    return PlanInputs(data_dir, desired, state, table, ignore)


@dataclass
class DeployPlanning:
    plan: DeployPlan[LinearCustomer, LiveCustomer]
    conflicts: list[str] = field(default_factory=list)  # ignore conflicts: refuse
    reports: list[str] = field(default_factory=list)  # stubs of ignored customers: note


def load_retire_file(data_dir: Path) -> dict[str, RetireEntry]:
    return load_retire(data_dir / RETIRE_FILE, LINEAR_SCHEMA)


def plan_for_deploy(
    inp: PlanInputs,
    all_live: list[LiveCustomer],
    scope: str | None,
    *,
    retire: dict[str, RetireEntry] | None = None,
) -> DeployPlanning:
    """`deploy`'s plan against `all_live`, every live customer Linear lists. `retire`
    defaults to `linear-retire.yaml`."""
    retire = retire if retire is not None else load_retire_file(inp.data_dir)
    managed, ignored, other = partitioned(all_live, inp.ignore, scope)
    conflicts, reports = ignore_conflicts(
        ignored, inp.ignore, entries(inp.data_dir), inp.desired, inp.table, scope=scope
    )
    plan = plan_deploy(
        inp.desired, managed, inp.table, state=inp.state, retire=retire, other_live=other
    )
    return DeployPlanning(plan, conflicts, reports)


def check_for(inp: PlanInputs, all_live: list[LiveCustomer], scope: str | None) -> CheckResult:
    """`check`'s `run_check` against `all_live`, every live customer Linear lists."""
    retire = load_retire_file(inp.data_dir)
    if scope:
        # a scoped run only sees its own rows; keep retire entries for those rows
        in_scope = {d.external_ids[0] for d in inp.desired if d.external_ids}
        retire = {k: v for k, v in retire.items() if k in in_scope}
    live = scope_live(all_live, scope)
    in_scope_ids = {lv.id for lv in live}
    return run_check(
        inp.desired,
        inp.state,
        live,
        inp.table,
        retire=retire,
        ignore=inp.ignore,
        scope=scope,
        entries=entries(inp.data_dir),
        other_live=[lv for lv in all_live if lv.id not in in_scope_ids],
    )


def check_exit(res: CheckResult, offline: list[str]) -> int:
    """`check`'s exit: the live checks' code, else EXIT_DRIFT on any offline error."""
    return res.code or (EXIT_DRIFT if offline else 0)


def offline_plan(
    data_dir: Path,
    *,
    scope: str | None = None,
    live: list[LiveCustomer] | None = None,
) -> tuple[DeployPlan[LinearCustomer, LiveCustomer], int]:
    """`deploy`'s plan and `check`'s exit code, with no Linear access: `live` (default:
    every state-mirror row as the customer it records) stands in for Linear.

    Raises ValueError when the identity table or ignore file cannot be read."""
    data_dir = Path(data_dir)
    inp = load_inputs(data_dir, scope)
    if inp is None:
        raise ValueError(f"cannot plan {data_dir}: see the error above")
    if live is None:
        live = [state_to_live(r) for r in load_state(data_dir) if r.linear_synced is not None]
    planning = plan_for_deploy(inp, live, scope)
    res = check_for(inp, live, scope)
    return planning.plan, check_exit(res, offline_errors(data_dir))
