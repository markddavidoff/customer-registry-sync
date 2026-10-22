"""Generate: project the Layer-1 dataset onto Linear's schema.

Maps each dataset record to a `LinearCustomer` (Linear-supported fields only):
classification→status, arr→revenue(int), tier passthrough, and `external_ids`
carrying our canonical slug as the stable dataset↔Linear link. `internal`
records are not projected. Produces desired config only; `linear_synced` is
never emitted (state lives in `customers/linear-state/`).
"""

from __future__ import annotations

import re

from .linear_schema import LinearCustomer
from .schema import CustomerRecord
from .writers import slug_for

# classification → Linear status. None = not projected to Linear.
STATUS_MAP: dict[str | None, str | None] = {
    "real": "Active",
    "partner": "Active",
    "prospect": "Prospect",
    "churned": "Churned",
    "internal": None,
    None: None,
}

# Linear's customer search does not surface status, so every managed customer's
# display name carries it: "<name> [<status>]".
_STATUS_SUFFIX_RE = re.compile(r" \[(?:Active|Prospect|Churned)\]$")


def strip_status_suffix(name: str) -> str:
    """Remove a trailing ` [Active]` / ` [Prospect]` / ` [Churned]`, if present."""
    stripped = _STATUS_SUFFIX_RE.sub("", name)
    return stripped or name


def generate_linear(dataset_recs: list[CustomerRecord]) -> list[LinearCustomer]:
    """Project the dataset onto Linear's schema. Desired only — no sync state;
    state lives in customers/linear-state/ (see writers.load_state)."""
    out: list[LinearCustomer] = []
    for rec in dataset_recs:
        status = STATUS_MAP.get(rec.classification)
        if status is None:
            continue  # internal / unclassified → not a Linear customer
        revenue = int(round(rec.arr)) if rec.arr is not None else None
        out.append(
            LinearCustomer(
                name=f"{rec.canonical_name} [{status}]",
                domains=list(rec.domains),
                revenue=revenue,
                tier=rec.tier,
                status=status,
                external_ids=[slug_for(rec)],
            )
        )
    out.sort(key=lambda x: x.name.lower())
    return out
