"""Inbound sync: mirror live Linear Customers into Layer 2.

Reads live Linear and reconciles `<data_dir>/linear-state/*` so the repo tracks
reality, refreshing each file's `linear_synced` (the optimistic-concurrency
pre-state). Every field of a mirror row comes from its live customer, and nothing
else: the old state row for the same Linear id has nothing to carry over, and a
state row is never matched by domain or name (that would hand one customer's
canonical id to a second customer sharing its domain). A live customer with no
status is mirrored with no status (`None`), so its stub stays unclassified and
`check` names it. Deterministic + idempotent.
"""

from __future__ import annotations

from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer, LinearSynced, content_hash_of


def reconcile_inbound(
    live: list[LiveCustomer],
    existing_state: list[LinearCustomer],
) -> list[LinearCustomer]:
    """The mirror of `live`. `existing_state` is accepted for the callers' symmetry
    but contributes nothing: each row is a faithful copy of its live customer."""
    del existing_state
    out: list[LinearCustomer] = []
    for lc in live:
        rec = LinearCustomer(
            name=lc.name,
            domains=list(lc.domains),
            revenue=lc.revenue,
            tier=lc.tier,
            status=lc.status or None,
            external_ids=list(lc.external_ids),
        )
        rec.linear_synced = LinearSynced(
            linear_id=lc.id,
            updated_at=lc.updated_at,
            content_hash=content_hash_of(rec),
        )
        out.append(rec)
    out.sort(key=lambda x: x.name.lower())
    return out
