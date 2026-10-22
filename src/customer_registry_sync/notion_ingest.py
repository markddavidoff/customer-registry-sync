"""Deterministic extractor for a hub of customer pages in Notion.

Each customer page is machine-generated from a consistent template:
``heading_2`` sections (Company Overview, Account Relationship, Active Deals,
Key Contacts, …) whose ``bulleted_list_item`` children are ``Label: value``
pairs. We walk the blocks, build a first-wins ``Label -> value`` map, and
map the labels we know onto a :class:`RawCustomer`. Unknown values stay
``None`` — never invented. Classification is derived from the *bucket*, not
the page's own (often messy) Status line.

The parse is deterministic, not LLM-based: the pages are machine-generated and
uniform. ``docs/notion-template.md`` documents the layout the parser expects.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from .schema import Classification, Deployment, RawCustomer

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
