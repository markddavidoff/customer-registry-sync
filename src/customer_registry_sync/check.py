"""`check` — a read-only gate a CI job can run on each pull request (exit 3 fails it).

The drift and combined-check logic now lives in `gitops_engine`; this module is
a thin Linear-specific wrapper that supplies the `LinearCustomersAdapter`.

Drift = the repo's recorded picture of Linear (customers/linear-state/) no
longer matches live, judged by `content_hash` of the deployable fields (NOT
`updatedAt`, which Linear bumps on edits to fields we ignore). Three classes:
changed (state≠live), added (live row with no state row), deleted (state row
with no live row). Exit 3 on any drift or plan-time abort; 0 when in sync.
"""

from __future__ import annotations

from gitops_engine import (
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_OPERROR,
    DriftReport,
    Plan,
)
from gitops_engine import (
    detect_drift as _engine_detect_drift,
)
from gitops_engine import (
    run_check as _engine_run_check,
)

from .identity import IdentityTable
from .linear_adapter import LinearCustomersAdapter
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer

__all__ = [
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXIT_OPERROR",
    "DriftReport",
    "detect_drift",
    "run_check",
]


def detect_drift(state: list[LinearCustomer], live: list[LiveCustomer]) -> DriftReport:
    """Whether the recorded state mirror still matches live, by content hash.
    Read-only: the adapter's client is never touched."""
    adapter = LinearCustomersAdapter()
    return _engine_detect_drift(state, live, adapter)


def run_check(
    desired: list[LinearCustomer],
    state: list[LinearCustomer],
    live: list[LiveCustomer],
    table: IdentityTable | None = None,
) -> tuple[int, DriftReport, Plan[LinearCustomer, LiveCustomer]]:
    """Report drift and a plan together. EXIT_DRIFT when the state no longer
    matches live OR the plan would abort; EXIT_OK when everything reconciles.
    Desired rows match live customers through the identity `table`."""
    adapter = LinearCustomersAdapter(table=table)
    return _engine_run_check(desired, state, live, adapter)
