"""Shared record types for the customer registry.

`RawCustomer` is what the Notion extractor emits per customer page/row
(one bucket, un-merged). `CustomerRecord` is a dataset row: the assembled
Notion record after the cross-bucket merge, or a `linear-found` stub. Unknown
values stay ``None`` /
empty — never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal

Deployment = Literal["saas", "on-prem", "hybrid"]
Classification = Literal["real", "prospect", "churned", "partner", "internal"]

VALID_DEPLOYMENTS: frozenset[str | None] = frozenset({"saas", "on-prem", "hybrid", None})
VALID_CLASSIFICATIONS: frozenset[str | None] = frozenset(
    {"real", "prospect", "churned", "partner", "internal", None}
)


@dataclass
class CustomerRecord:
    """One assembled row of the customer dataset."""

    canonical_name: str
    domains: list[str] = field(default_factory=list)
    buckets: list[str] = field(default_factory=list)
    deployment: Deployment | None = None
    classification: Classification | None = None
    arr: float | None = None
    tier: str | None = None
    status: str | None = None
    owner: str | None = None
    contact: str | None = None
    prod_org_id: str | None = None
    hubspot_id: str | None = None
    linear_customer_id: str | None = None
    sources: list[str] = field(default_factory=list)
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.canonical_name:
            raise ValueError("canonical_name is required")
        if self.deployment not in VALID_DEPLOYMENTS:
            raise ValueError(f"invalid deployment {self.deployment!r}")
        if self.classification not in VALID_CLASSIFICATIONS:
            raise ValueError(f"invalid classification {self.classification!r}")

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomerRecord:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown key(s): {sorted(unknown)}")
        return cls(**data)


@dataclass
class RawCustomer:
    """Per-page extraction from one Notion bucket, before merge."""

    name: str
    bucket: str
    domains: list[str] = field(default_factory=list)
    deployment: Deployment | None = None
    classification: Classification | None = None
    arr: float | None = None
    tier: str | None = None
    status: str | None = None
    owner: str | None = None
    contact: str | None = None
    prod_org_id: str | None = None
    hubspot_id: str | None = None
    source_page_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name is required")
        if self.deployment not in VALID_DEPLOYMENTS:
            raise ValueError(f"invalid deployment {self.deployment!r}")
        if self.classification not in VALID_CLASSIFICATIONS:
            raise ValueError(f"invalid classification {self.classification!r}")
