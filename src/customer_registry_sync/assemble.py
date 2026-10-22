"""Normalize + merge extracted + curated records into the dataset.

Entities are keyed domain-first then by normalized name, so the same company
appearing in several buckets (or under an alias) collapses to one record.
Classification follows bucket precedence (real > partner > churned > prospect
> internal); a hand-curated override wins over all of it. Curated self-managed
deployment wins over whatever a page stated.
"""

from __future__ import annotations

import re
from typing import Any

from .notion_ingest import classification_for_bucket
from .schema import Classification, CustomerRecord, RawCustomer

CLASS_RANK: dict[Classification | None, int] = {
    "real": 5,
    "partner": 4,
    "churned": 3,
    "prospect": 2,
    "internal": 1,
    None: 0,
}

_SELF_MANAGED_SOURCE = "self-managed-customers.yaml"


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _raw_to_record(r: RawCustomer) -> CustomerRecord:
    return CustomerRecord(
        canonical_name=r.name,
        domains=list(r.domains),
        buckets=[r.bucket],
        classification=classification_for_bucket(r.bucket),  # bucket is authoritative
        deployment=r.deployment,
        arr=r.arr,
        tier=r.tier,
        status=r.status,
        contact=r.contact,
        hubspot_id=r.hubspot_id,
        prod_org_id=r.prod_org_id,
        sources=[f"notion:{r.bucket}"],
    )


def _is_self_managed(rec: CustomerRecord) -> bool:
    return _SELF_MANAGED_SOURCE in rec.sources


def _keys(rec: CustomerRecord) -> set[str]:
    return {d for d in rec.domains} | {"name:" + _norm_name(rec.canonical_name)}


def _union(seq: list[str], extra: list[str]) -> list[str]:
    out = list(seq)
    for x in extra:
        if x not in out:
            out.append(x)
    return out


def _first_non_null(records: list[CustomerRecord], attr: str) -> Any:
    for rec in records:
        val = getattr(rec, attr)
        if val is not None:
            return val
    return None


def _merge_group(members: list[CustomerRecord]) -> CustomerRecord:
    notion = [m for m in members if not _is_self_managed(m)]
    self_managed = [m for m in members if _is_self_managed(m)]
    # highest classification rank first; tie → longer name; then input order
    notion_sorted = sorted(
        notion, key=lambda r: (-CLASS_RANK[r.classification], -len(r.canonical_name))
    )
    ordered = notion_sorted + self_managed

    # The Notion bucket is authoritative when present; a self-managed-only group (no Notion
    # page) falls back to the curated record's name + classification so it is not dropped.
    primary = notion_sorted[0] if notion_sorted else self_managed[0]
    name = primary.canonical_name
    classification = primary.classification

    deployment = _first_non_null(self_managed, "deployment") or _first_non_null(
        ordered, "deployment"
    )

    domains: list[str] = []
    buckets: list[str] = []
    sources: list[str] = []
    notes: list[str] = []
    for m in ordered:
        domains = _union(domains, m.domains)
        buckets = _union(buckets, m.buckets)
        sources = _union(sources, m.sources)
        if m.notes:
            notes.append(m.notes)

    return CustomerRecord(
        canonical_name=name,
        domains=domains,
        buckets=buckets,
        classification=classification,
        deployment=deployment,
        arr=_first_non_null(ordered, "arr"),
        tier=_first_non_null(ordered, "tier"),
        status=_first_non_null(ordered, "status"),
        owner=_first_non_null(ordered, "owner"),
        contact=_first_non_null(ordered, "contact"),
        prod_org_id=_first_non_null(ordered, "prod_org_id"),
        hubspot_id=_first_non_null(ordered, "hubspot_id"),
        linear_customer_id=_first_non_null(ordered, "linear_customer_id"),
        sources=sources,
        notes="; ".join(notes) or None,
    )


def _apply_overrides(records: list[CustomerRecord], overrides: dict[str, dict[str, Any]]) -> None:
    for rec in records:
        for key in [*rec.domains, rec.canonical_name]:
            ov = overrides.get(key)
            if not ov:
                continue
            new_class = ov.get("classification")
            if new_class:
                rec.classification = new_class
            reason = ov.get("reason")
            note = f"override: classification={new_class}" + (f" ({reason})" if reason else "")
            rec.notes = f"{rec.notes}; {note}" if rec.notes else note
            break


def assemble_dataset(
    raw: list[RawCustomer],
    self_managed: list[CustomerRecord],
    overrides: dict[str, dict[str, Any]],
) -> list[CustomerRecord]:
    records = [_raw_to_record(r) for r in raw] + list(self_managed)

    groups: list[dict[str, Any]] = []
    key_to_group: dict[str, dict[str, Any]] = {}
    for rec in records:
        ks = _keys(rec)
        found: list[dict[str, Any]] = []
        for k in ks:
            g = key_to_group.get(k)
            if g is not None and g not in found:
                found.append(g)
        if not found:
            group: dict[str, Any] = {"keys": set(), "members": []}
            groups.append(group)
        else:
            group = found[0]
            for other in found[1:]:  # transitively merge groups that share a key
                group["keys"] |= other["keys"]
                group["members"] += other["members"]
                for k2 in other["keys"]:
                    key_to_group[k2] = group
                other["members"] = []
        group["keys"] |= ks
        group["members"].append(rec)
        for k in ks:
            key_to_group[k] = group

    merged = [_merge_group(g["members"]) for g in groups if g["members"]]
    _apply_overrides(merged, overrides)
    merged.sort(key=lambda r: _norm_name(r.canonical_name))
    return merged
