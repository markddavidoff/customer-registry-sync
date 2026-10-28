"""The Notion source adapter.

Notion hub of customer pages -> ``list[CustomerRecord]``: the deterministic page
extractor, the cross-bucket merge, and the curated inputs the merge reads.

Each customer page is machine-generated from a consistent template:
``heading_2`` sections (Company Overview, Account Relationship, Active Deals,
Key Contacts, ...) whose ``bulleted_list_item`` children are ``Label: value``
pairs. We walk the blocks, build a first-wins ``Label -> value`` map, and
map the labels we know onto a :class:`RawCustomer`. Unknown values stay
``None``, never invented. Classification is derived from the *bucket*, not
the page's own (often messy) Status line.

The parse is deterministic, not LLM-based: the pages are machine-generated and
uniform. ``docs/notion-template.md`` documents the layout the parser expects.

The merge keys entities domain-first then by normalized name, so the same company
appearing in several buckets (or under an alias) collapses to one record.
Classification follows bucket precedence (real > partner > churned > prospect
> internal); a hand-curated override wins over all of it. Curated self-managed
deployment wins over whatever a page stated.

Hand-curated inputs, merged but never overwritten:

- ``self-managed-customers.yaml``: on-prem/self-managed deployment registry.
- ``classification-overrides.yaml``: per-domain/name classification overrides.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Protocol

import yaml

from .base import SourceConfigError, SourceError
from ..schema import (
    VALID_CLASSIFICATIONS,
    VALID_DEPLOYMENTS,
    Classification,
    CustomerRecord,
    Deployment,
    RawCustomer,
)

# bucket name -> classification (Archive holds docs, not customers → internal)
BUCKET_CLASSIFICATION: dict[str, Classification] = {
    "Customers": "real",
    "Churned Customers": "churned",
    "Archive": "internal",
    "POCs": "prospect",
    "Prospects": "prospect",
    "Partner & Supporting Accounts": "partner",
}

# Titles under a bucket that are methodology/reference pages, not customers.
# Customize this list for your own workspace's reference-page titles.
NON_CUSTOMER_TITLE_MARKERS = (
    "methodology",
    "health report",
    "start here",
    "engagement summaries",
    "document structure",
    "onboarding guide",
)
# nested sub-bucket pages that are enumerated separately, not as customers
NESTED_BUCKET_TITLES = ("archive", "churned customers")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", re.IGNORECASE)
_MONEY_MULT = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}
_DIGITS = re.compile(r"(\d{4,})")
_CONTACT_SPLIT = re.compile(r"\s+[-(]")
_CONTACT_NOISE_PREFIXES = ("last synced", "source:", "maintained by", "note:")


def _clean_contact_line(text: str) -> str | None:
    """A contacts-section line → an email, a bare name, or None for metadata."""
    low = text.lower()
    if text.startswith("[") or low.startswith(_CONTACT_NOISE_PREFIXES):
        return None
    em = _EMAIL.search(text)
    if em:
        return em.group(0)
    name = _CONTACT_SPLIT.split(text, maxsplit=1)[0].strip()
    return name or None


def _clean_tier(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split("|")[0].strip()
    return None if first.lower() in ("none", "n/a", "") else first


def classification_for_bucket(bucket: str) -> Classification | None:
    return BUCKET_CLASSIFICATION.get(bucket)


def norm_arr(value: str | None) -> float | None:
    if not value:
        return None
    m = _MONEY.search(value)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").upper()
    return num * _MONEY_MULT.get(suffix, 1.0)


def norm_deployment(value: str | None) -> Deployment | None:
    if not value:
        return None
    v = value.lower()
    on_prem = any(t in v for t in ("self-managed", "self managed", "on-prem", "on prem", "air-gap"))
    saas = "saas" in v
    if "hybrid" in v or (on_prem and saas):
        return "hybrid"
    if on_prem:
        return "on-prem"
    if saas:
        return "saas"
    return None


def _rich_text(block: dict[str, Any]) -> str:
    node = block.get(block["type"], {})
    if isinstance(node, dict) and "rich_text" in node:
        return "".join(x.get("plain_text", "") for x in node["rich_text"]).strip()
    return ""


def _field_map(blocks: list[dict[str, Any]]) -> tuple[dict[str, str], str | None]:
    """First-wins ``Label -> value`` map from bulleted items, plus the first
    Key-Contacts email (or contact text)."""
    fields: dict[str, str] = {}
    contact: str | None = None
    in_contacts = False
    for b in blocks:
        bt = b["type"]
        if bt.startswith("heading_"):
            in_contacts = "contact" in _rich_text(b).lower()
            continue
        if bt not in ("bulleted_list_item", "numbered_list_item", "paragraph"):
            continue
        text = _rich_text(b)
        if not text:
            continue
        if in_contacts and contact is None:
            contact = _clean_contact_line(text)
        if ": " in text:
            label, _, value = text.partition(": ")
            label = label.strip()
            if label and len(label.split()) <= 3 and label not in fields:
                fields[label.strip()] = value.strip()
    return fields, contact


def _domains(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for part in re.split(r"[,\s;]+", value):
        part = part.strip().lower()
        if "." in part and " " not in part:
            out.append(part)
    return out


def parse_customer_page(
    blocks: list[dict[str, Any]], *, bucket: str, page_id: str, title: str
) -> RawCustomer:
    fields, contact = _field_map(blocks)
    hubspot_raw = fields.get("HubSpot ID")
    hubspot_id = None
    if hubspot_raw:
        m = _DIGITS.search(hubspot_raw)
        hubspot_id = m.group(1) if m else None
    return RawCustomer(
        name=title,
        bucket=bucket,
        classification=classification_for_bucket(bucket),
        domains=_domains(fields.get("Domain")),
        deployment=norm_deployment(fields.get("Deployment")),
        arr=norm_arr(fields.get("ARR")),  # only the explicit ARR line, not Annual Revenue
        tier=_clean_tier(fields.get("Tier")),
        status=fields.get("Status"),
        contact=contact,
        hubspot_id=hubspot_id,
        source_page_id=page_id,
        extra={
            k: v
            for k, v in fields.items()
            if k in ("Annual Revenue", "Industry", "Slack Channel", "Parent Company")
        },
    )


def parse_partner_table(rows: list[dict[str, Any]], *, bucket: str) -> list[RawCustomer]:
    """Parse the Partner & Supporting Accounts ``table`` block. Row 1 is the
    header (Account, Contact, Reason Kept Active, Flagged By, Status)."""
    out: list[RawCustomer] = []
    for i, row in enumerate(rows):
        cells = [
            " ".join(t.get("plain_text", "") for t in cell).strip()
            for cell in row["table_row"]["cells"]
        ]
        if not cells or not cells[0]:
            continue
        if i == 0 and cells[0].lower() == "account":
            continue  # header
        account = cells[0]
        contact = cells[1] if len(cells) > 1 else ""
        status = cells[4] if len(cells) > 4 else ""
        out.append(
            RawCustomer(
                name=account,
                bucket=bucket,
                classification=classification_for_bucket(bucket),
                contact=None if contact in ("", "—", "-") else contact,
                status=status or None,
                extra={
                    "reason_kept_active": cells[2] if len(cells) > 2 else "",
                    "flagged_by": cells[3] if len(cells) > 3 else "",
                },
            )
        )
    return out


# ---- live orchestration (not exercised by unit tests; --live smoke only) ----


class _Notion(Protocol):
    def blocks(self) -> Any: ...


def _list_children(client: Any, block_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        resp = client.blocks.children.list(block_id, start_cursor=cursor, page_size=100)
        out.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return out


def _is_customer_page(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return False
    if low in NESTED_BUCKET_TITLES:
        return False
    return not any(m in low for m in NON_CUSTOMER_TITLE_MARKERS)


def extract_customers(client: Any, buckets: dict[str, str]) -> list[RawCustomer]:
    """Live extraction over every bucket (name -> Notion page id)."""
    records: list[RawCustomer] = []
    for bucket, page_id in buckets.items():
        children = _list_children(client, page_id)
        for b in children:
            if b["type"] == "child_page":
                title = b["child_page"]["title"]
                if not _is_customer_page(title):
                    continue
                page_blocks = _list_children(client, b["id"])
                records.append(
                    parse_customer_page(page_blocks, bucket=bucket, page_id=b["id"], title=title)
                )
            elif b["type"] == "table" and bucket == "Partner & Supporting Accounts":
                rows = _list_children(client, b["id"])
                records.extend(parse_partner_table(rows, bucket=bucket))
    return records


# ---- merge: extracted + curated records -> dataset ----

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


# ---- curated inputs ----


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


# ---- the adapter ----

SELF_MANAGED_FILE = "self-managed-customers.yaml"
OVERRIDES_FILE = "classification-overrides.yaml"


def _require_str(section: Mapping[str, Any], key: str, label: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceConfigError(f"config is missing required key '{label}'")
    return value.strip()


class NotionSource:
    """Notion hub -> customer records. Picked by ``source: {kind: notion, ...}``."""

    name: ClassVar[str] = "notion"

    def __init__(self, hub_page_id: str, buckets: dict[str, str]) -> None:
        self.hub_page_id = hub_page_id
        self.buckets = buckets

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> NotionSource:
        hub_page_id = _require_str(section, "hub_page_id", "source.hub_page_id")
        raw_buckets = section.get("buckets")
        if not isinstance(raw_buckets, dict):
            raise SourceConfigError("config is missing required key 'source.buckets'")
        for bucket in raw_buckets:
            if bucket not in BUCKET_CLASSIFICATION:
                raise SourceConfigError(
                    f"config has unknown bucket '{bucket}' under 'source.buckets'"
                )
        buckets = {
            bucket: _require_str(raw_buckets, bucket, f"source.buckets.{bucket}")
            for bucket in BUCKET_CLASSIFICATION
        }
        return cls(hub_page_id, buckets)

    def ingest(self, data_dir: Path) -> list[CustomerRecord]:
        """Read the Notion pages and merge the curated inputs found in ``data_dir``."""
        token = os.environ.get("NOTION_API_TOKEN")
        if not token:
            raise SourceError("NOTION_API_TOKEN not set (set it in the environment).")
        from notion_client import Client  # lazy so unit tests need no network

        raw = extract_customers(Client(auth=token), self.buckets)
        self_managed_path = data_dir / SELF_MANAGED_FILE
        overrides_path = data_dir / OVERRIDES_FILE
        self_managed = (
            load_self_managed_customers(self_managed_path) if self_managed_path.exists() else []
        )
        overrides = load_overrides(overrides_path) if overrides_path.exists() else {}
        return assemble_dataset(raw, self_managed, overrides)
