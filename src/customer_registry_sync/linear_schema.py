"""Layer-2 record types: Linear-Customers-as-code.

`LinearCustomer` is the deployable projection (only fields Linear supports)
plus a `linear_synced` block recording the exact Linear state the file was
last reconciled against — the basis for optimistic concurrency.
`content_hash_of` hashes the deployable fields only (never `linear_synced`),
so it answers "did the thing we deploy change?".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any

from gitops_engine.schema import Nouns

if TYPE_CHECKING:
    from gitops_engine.identity import ItemKeys

    from .linear_client import LiveCustomer

LinearStatus = str
# The deployable statuses generate emits. A workspace can define more, which a
# mirror row may legitimately carry — so status is validated
# only as a non-empty label, not against this set.
VALID_LINEAR_STATUSES: frozenset[str] = frozenset({"Active", "Prospect", "Churned"})


@dataclass
class LinearSynced:
    linear_id: str
    updated_at: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LinearCustomer:
    name: str
    domains: list[str] = field(default_factory=list)
    revenue: int | None = None
    tier: str | None = None
    # None only on a state-mirror row whose Linear customer has no status;
    # `generate` always emits one.
    status: LinearStatus | None = "Active"
    external_ids: list[str] = field(default_factory=list)
    linear_synced: LinearSynced | None = None
    # A `linear-found` stub's desired row: Linear owns its fields.
    linear_owned: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name is required")
        if self.status is not None and not self.status:
            raise ValueError("status is required (None for a Linear customer with no status)")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "domains": self.domains,
            "revenue": self.revenue,
            "tier": self.tier,
            "status": self.status,
            "external_ids": self.external_ids,
            "linear_synced": self.linear_synced.to_dict() if self.linear_synced else None,
        }
        if self.linear_owned:
            d["linear_owned"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinearCustomer:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown key(s): {sorted(unknown)}")
        ls = data.get("linear_synced")
        return cls(
            name=data["name"],
            domains=list(data.get("domains") or []),
            revenue=data.get("revenue"),
            tier=data.get("tier"),
            status=data.get("status", "Active"),
            external_ids=list(data.get("external_ids") or []),
            linear_synced=LinearSynced(**ls) if ls else None,
            linear_owned=bool(data.get("linear_owned", False)),
        )


def content_hash_of(rec: LinearCustomer) -> str:
    """sha256 over the deployable fields only (order-insensitive for lists);
    `linear_synced` and `linear_owned` are bookkeeping and never hashed."""
    payload = {
        "name": rec.name,
        "domains": sorted(rec.domains),
        "revenue": rec.revenue,
        "tier": rec.tier,
        "status": rec.status,
        "external_ids": sorted(rec.external_ids),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class LinearSchema:
    """The engine's view of Linear customers (`RecordSchema[LinearCustomer, LiveCustomer]`).

    Only the members the engine's merge and found-stub code call are here so far; the
    field table, policies and snapshot members arrive with the planner."""

    nouns = Nouns("customer", "customers", "Linear")

    def label(self, d: LinearCustomer) -> str:
        return d.name

    def live_label(self, lv: LiveCustomer) -> str:
        return lv.name

    def live_id(self, lv: LiveCustomer) -> str:
        return lv.id

    def desired_keys(self, d: LinearCustomer) -> ItemKeys:
        from .identity import keys_for_row  # identity imports this module for its types

        return keys_for_row(d)

    def live_keys(self, lv: LiveCustomer) -> ItemKeys:
        from .identity import keys_for_live  # identity imports this module for its types

        return keys_for_live(lv)


LINEAR_SCHEMA = LinearSchema()
