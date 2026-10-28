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
import dataclasses
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any

from gitops_engine.schema import Always, FieldSpec, KeepIfUnset, Nouns, SyncMark, Union

from .linear_client import LiveCustomer

if TYPE_CHECKING:
    from gitops_engine.identity import ItemKeys

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

    `nouns.version` names Linear's version token in drift messages."""

    nouns = Nouns("customer", "customers", "Linear", source="registry", version="updatedAt")
    fields = (
        FieldSpec("name", Always()),
        FieldSpec("status", Always(), required=True),
        FieldSpec("external_ids", Always(unordered=True)),
        FieldSpec("domains", Union(exclusive=True, item="domain")),
        FieldSpec("revenue", KeepIfUnset()),
        FieldSpec("tier", KeepIfUnset()),
    )
    id_field = "external_ids"
    snapshot_id_key = "linear_id"
    snapshot_version_key = "updated_at"

    def desired_value(self, d: LinearCustomer, field: str) -> Any:
        return getattr(d, field)

    def live_value(self, lv: LiveCustomer, field: str) -> Any:
        return getattr(lv, field)

    def label(self, d: LinearCustomer) -> str:
        return d.name

    def live_label(self, lv: LiveCustomer) -> str:
        return lv.name

    def live_id(self, lv: LiveCustomer) -> str:
        return lv.id

    def live_version(self, lv: LiveCustomer) -> str:
        return lv.updated_at

    def desired_keys(self, d: LinearCustomer) -> ItemKeys:
        from .identity import keys_for_row  # identity imports this module for its types

        return keys_for_row(d)

    def live_keys(self, lv: LiveCustomer) -> ItemKeys:
        from .identity import keys_for_live  # identity imports this module for its types

        return keys_for_live(lv)

    def mark(self, d: LinearCustomer) -> SyncMark | None:
        ls = d.linear_synced
        return SyncMark(ls.linear_id, ls.updated_at, ls.content_hash) if ls else None

    def destination_owned(self, d: LinearCustomer) -> bool:
        return d.linear_owned

    def with_values(self, d: LinearCustomer, values: dict[str, Any]) -> LinearCustomer:
        return dataclasses.replace(d, **values)

    def mirror_row(self, lv: LiveCustomer) -> LinearCustomer:
        """The live customer as the state mirror holds it: every field from `lv`, no
        status as None, and a `linear_synced` mark."""
        rec = LinearCustomer(
            name=lv.name,
            domains=list(lv.domains),
            revenue=lv.revenue,
            tier=lv.tier,
            status=lv.status or None,
            external_ids=list(lv.external_ids),
        )
        rec.linear_synced = LinearSynced(
            linear_id=lv.id,
            updated_at=lv.updated_at,
            content_hash=content_hash_of(rec),
        )
        return rec

    def live_hash(self, lv: LiveCustomer) -> str:
        return content_hash_of(self.mirror_row(lv))

    def state_to_live(self, s: LinearCustomer) -> LiveCustomer:
        """A `LiveCustomer` view of a state-mirror row (id/updatedAt from `linear_synced`)."""
        ls = s.linear_synced
        return LiveCustomer(
            id=ls.linear_id if ls else "",
            name=s.name,
            domains=list(s.domains),
            revenue=s.revenue,
            tier=s.tier,
            status=s.status or "",
            external_ids=list(s.external_ids),
            updated_at=ls.updated_at if ls else "",
        )

    def snapshot_row(self, lv: LiveCustomer) -> dict[str, Any]:
        """A snapshot row, keys in the order v0.1.0 snapshots hold them."""
        return {
            "linear_id": lv.id,
            "name": lv.name,
            "domains": list(lv.domains),
            "revenue": lv.revenue,
            "tier": lv.tier,
            "status": lv.status,
            "external_ids": list(lv.external_ids),
            "updated_at": lv.updated_at,
        }

    def snapshot_values(self, row: dict[str, Any]) -> dict[str, Any]:
        """What `restore` sends for a snapshot row: every managed field, except an empty
        status (Linear cannot clear one)."""
        send = {
            "name": row["name"],
            "domains": list(row["domains"]),
            "external_ids": list(row["external_ids"]),
            "revenue": row["revenue"],
            "tier": row["tier"],
        }
        if row["status"]:
            send["status"] = row["status"]
        return send


LINEAR_SCHEMA = LinearSchema()
