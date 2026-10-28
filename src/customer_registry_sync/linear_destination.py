"""The engine's destination for Linear Customers.

`LinearDestination` turns the engine's writes, dicts keyed by `LINEAR_SCHEMA` field
names, into `LinearClient` calls: `status` goes out as `status_id` and `tier` as
`tier_id`, both resolved through the workspace's status and tier tables. `preflight`
resolves every status and tier a run writes before the first write.
"""

from __future__ import annotations

from typing import Any

from .linear_client import LiveCustomer

# `create_customer` keyword order, as v0.1.0 sent it.
_CREATE_ORDER = ("name", "domains", "external_ids", "status", "tier", "revenue")


class LinearDestination:
    """`gitops_engine.destination.Destination[LiveCustomer]` over a `LinearClient`."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._status_ids: dict[str, str] | None = None
        self._tier_ids: dict[str, str] | None = None

    def _ids(self) -> tuple[dict[str, str], dict[str, str]]:
        if self._status_ids is None or self._tier_ids is None:
            self._status_ids = self._client.status_ids()
            self._tier_ids = self._client.tier_ids()
        return self._status_ids, self._tier_ids

    def list_live(self) -> list[LiveCustomer]:
        return self._client.list_customers()

    def get(self, live_id: str) -> LiveCustomer | None:
        return self._client.get_customer(live_id)

    def preflight(self, writes: list[dict[str, Any]], *, restoring: bool = False) -> None:
        """Every status and tier the writes carry must resolve to an id before any
        write; otherwise an unresolvable name mid-run would raise after earlier writes
        already reached Linear."""
        self._status_ids = self._tier_ids = None
        status_ids, tier_ids = self._ids()
        if restoring:
            missing = sorted(
                {w["status"] for w in writes if w.get("status") and w["status"] not in status_ids}
                | {w["tier"] for w in writes if w.get("tier") and w["tier"] not in tier_ids}
            )
            if missing:
                raise ValueError(f"unresolvable Linear status/tier, nothing restored: {missing}")
            return
        missing = sorted({w["status"] for w in writes if "status" in w} - set(status_ids))
        if missing:
            raise ValueError(f"unresolvable Linear status(es), nothing written: {missing}")
        missing = sorted({w["tier"] for w in writes if w.get("tier")} - set(tier_ids))
        if missing:
            raise ValueError(f"unresolvable Linear tier(s), nothing written: {missing}")

    def _kwargs(self, values: dict[str, Any], order: tuple[str, ...] | None = None) -> dict:
        status_ids, tier_ids = self._ids()
        out: dict[str, Any] = {}
        for k in order or tuple(values):
            if k not in values:
                continue
            v = values[k]
            if k == "status":
                out["status_id"] = status_ids[v]
            elif k == "tier":
                out["tier_id"] = tier_ids[v] if v else None
            else:
                out[k] = list(v) if isinstance(v, list) else v
        return out

    def create(self, values: dict[str, Any]) -> LiveCustomer:
        return self._client.create_customer(**self._kwargs(values, _CREATE_ORDER))

    def update(self, live_id: str, values: dict[str, Any], *, retire: bool) -> LiveCustomer:
        return self._client.update_customer(live_id, retire=retire, **self._kwargs(values))
