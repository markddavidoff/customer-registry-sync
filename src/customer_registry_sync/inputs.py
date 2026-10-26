"""Loaders for the hand-curated inputs the ingest merges but never overwrites.

- ``self-managed-customers.yaml`` — on-prem/self-managed deployment registry.
- ``classification-overrides.yaml`` — per-domain/name classification overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import VALID_CLASSIFICATIONS, VALID_DEPLOYMENTS, CustomerRecord


def load_self_managed_customers(path: str | Path) -> list[CustomerRecord]:
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    out: list[CustomerRecord] = []
    for entry in data.get("customers") or []:
        entry = entry or {}
        name = entry.get("name")
        if not name:
            raise ValueError(f"self-managed-customers entry missing 'name': {entry!r}")
        deployment = entry.get("deployment")
        if deployment not in VALID_DEPLOYMENTS:
            raise ValueError(f"invalid deployment {deployment!r} for {name!r}")
        # self-managed customers are paying customers by default; an self-managed-only entry (no
        # Notion page) must still land in real-customers.csv.
        classification = entry.get("classification", "real")
        if classification not in VALID_CLASSIFICATIONS:
            raise ValueError(f"invalid classification {classification!r} for {name!r}")
        out.append(
            CustomerRecord(
                canonical_name=name,
                domains=list(entry.get("domains") or []),
                deployment=deployment,
                classification=classification,
                notes=entry.get("notes"),
                sources=["self-managed-customers.yaml"],
            )
        )
    return out


def load_overrides(path: str | Path) -> dict[str, dict[str, Any]]:
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("overrides") or []:
        entry = entry or {}
        match = entry.get("match")
        if not match:
            raise ValueError(f"override entry missing 'match': {entry!r}")
        classification = entry.get("classification")
        if classification is not None and classification not in VALID_CLASSIFICATIONS:
            raise ValueError(f"invalid classification {classification!r} for {match!r}")
        out[match] = {k: v for k, v in entry.items() if k != "match"}
    return out
