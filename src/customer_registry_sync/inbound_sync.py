"""Inbound sync: mirror live Linear Customers into Layer 2.

Reads live Linear and reconciles `customers/linear-state/*` so the repo tracks
reality, refreshing each file's `linear_synced` (the optimistic-concurrency
pre-state). Fields come from LIVE Linear; where a live row matches an existing
Layer-2 entry (by external-id, then domain, then normalized name) we preserve
our canonical slug in `external_ids`. Unmatched live rows become new mirror
entries. Deterministic + idempotent.
"""

from __future__ import annotations

import re

from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer, LinearSynced, content_hash_of


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _match(live: LiveCustomer, existing: list[LinearCustomer]) -> LinearCustomer | None:
    live_xids = set(live.external_ids)
    if live_xids:
        for e in existing:
            if live_xids & set(e.external_ids):
                return e
    live_domains = set(live.domains)
    if live_domains:
        for e in existing:
            if live_domains & set(e.domains):
                return e
    ln = _norm(live.name)
    for e in existing:
        if _norm(e.name) == ln:
            return e
    return None


def reconcile_inbound(
    live: list[LiveCustomer],
    existing_state: list[LinearCustomer],
) -> list[LinearCustomer]:
    out: list[LinearCustomer] = []
    for lc in live:
        match = _match(lc, existing_state)
        external_ids = (
            list(match.external_ids) if match and match.external_ids else list(lc.external_ids)
        )
        rec = LinearCustomer(
            name=lc.name,
            domains=list(lc.domains),
            revenue=lc.revenue,
            tier=lc.tier,
            status=lc.status or None,  # an empty Linear status is mirrored as none
            external_ids=external_ids,
        )
        rec.linear_synced = LinearSynced(
            linear_id=lc.id,
            updated_at=lc.updated_at,
            content_hash=content_hash_of(rec),
        )
        out.append(rec)
    out.sort(key=lambda x: x.name.lower())
    return out
