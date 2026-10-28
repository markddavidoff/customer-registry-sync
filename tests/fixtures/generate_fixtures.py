"""Generate the synthetic test fixtures.

Every fixture is invented: fictional companies, ``example.test`` domains, and
random (seeded) UUIDs. Nothing is copied from a real workspace.

The *shape* follows what the producers emit, because the parser under test
(``sources.notion``) consumes that shape:

- Notion pages: the ``blocks.children.list`` result list. Each block carries
  ``object, id, parent, created_time, last_edited_time, created_by,
  last_edited_by, has_children, in_trash, type, <type node>, archived``.
  Text-bearing blocks hold ``rich_text`` segments (``type, text{content,
  link}, annotations{...}, plain_text, href``); ``table_row`` blocks hold
  ``cells`` (a list of rich_text lists).
- Linear: the GraphQL ``customers`` response for the field list in
  ``linear_client._CUSTOMER_FIELDS``.

Run from anywhere:  ``python tests/fixtures/generate_fixtures.py [out_dir]``
The output is deterministic (fixed seed), so re-running is a no-op.
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from pathlib import Path
from typing import Any

SEED = "customer-registry-sync/synthetic-fixtures/v1"
EDIT_TIME = "2026-03-18T10:14:00.000Z"
TABLE_TIME = "2026-03-20T15:41:00.000Z"
HEADING_TOGGLEABLE = False


class Ids:
    """Deterministic UUID source (one per fixture file)."""

    def __init__(self, label: str) -> None:
        self._rng = random.Random(f"{SEED}/{label}")

    def next(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))


# ---- rich text ---------------------------------------------------------------


def seg(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str = "default",
    link: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": text, "link": {"url": link} if link else None},
        "annotations": {
            "bold": bold,
            "italic": italic,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": color,
        },
        "plain_text": text,
        "href": link,
    }


# ---- blocks ------------------------------------------------------------------


class Page:
    """Builds the block list of one Notion page."""

    def __init__(self, label: str, *, when: str = EDIT_TIME) -> None:
        self.ids = Ids(label)
        self.page_id = self.ids.next()
        self.user_id = self.ids.next()
        self.when = when
        self.blocks: list[dict[str, Any]] = []

    def _block(self, type_: str, node: dict[str, Any], *, has_children: bool = False) -> None:
        self.blocks.append(
            {
                "object": "block",
                "id": self.ids.next(),
                "parent": {"type": "page_id", "page_id": self.page_id},
                "created_time": self.when,
                "last_edited_time": self.when,
                "created_by": {"object": "user", "id": self.user_id},
                "last_edited_by": {"object": "user", "id": self.user_id},
                "has_children": has_children,
                "in_trash": False,
                "type": type_,
                type_: node,
                "archived": False,
            }
        )

    def h2(self, text: str) -> Page:
        self._block(
            "heading_2",
            {"rich_text": [seg(text)], "is_toggleable": HEADING_TOGGLEABLE, "color": "default"},
        )
        return self

    def h3(self, text: str) -> Page:
        self._block(
            "heading_3",
            {"rich_text": [seg(text)], "is_toggleable": HEADING_TOGGLEABLE, "color": "default"},
        )
        return self

    def bullet(self, text: str) -> Page:
        self._block("bulleted_list_item", {"rich_text": [seg(text)], "color": "default"})
        return self

    def field(self, label: str, value: str) -> Page:
        """A ``Label: value`` bullet: bold label segment, plain value segment."""
        self._block(
            "bulleted_list_item",
            {"rich_text": [seg(f"{label}: ", bold=True), seg(value)], "color": "default"},
        )
        return self

    def rich_bullet(self, segments: list[dict[str, Any]]) -> Page:
        self._block("bulleted_list_item", {"rich_text": segments, "color": "default"})
        return self

    def numbered(self, text: str) -> Page:
        self._block("numbered_list_item", {"rich_text": [seg(text)], "color": "default"})
        return self

    def para(self, text: str) -> Page:
        self.rich_para([seg(text)])
        return self

    def rich_para(self, segments: list[dict[str, Any]]) -> Page:
        self._block("paragraph", {"rich_text": segments, "icon": None, "color": "default"})
        return self

    def callout(self, emoji: str, text: str, color: str = "default") -> Page:
        self._block(
            "callout",
            {
                "rich_text": [seg(text)],
                "icon": {"type": "emoji", "emoji": emoji},
                "color": color,
            },
        )
        return self

    def divider(self) -> Page:
        self._block("divider", {})
        return self

    def table(self, width: int) -> Page:
        """A table block. Its rows are separate children (see ``table_rows``)."""
        self._block(
            "table",
            {
                "table_width": width,
                "has_column_header": True,
                "has_row_header": False,
            },
            has_children=True,
        )
        return self

    def child_page(self, title: str) -> Page:
        self._block("child_page", {"title": title}, has_children=True)
        return self


def table_rows(label: str, rows: list[list[str]]) -> list[dict[str, Any]]:
    """The ``table_row`` children of one table block."""
    ids = Ids(label)
    table_id = ids.next()
    user_id = ids.next()
    out = []
    for cells in rows:
        out.append(
            {
                "object": "block",
                "id": ids.next(),
                "parent": {"type": "block_id", "block_id": table_id},
                "created_time": TABLE_TIME,
                "last_edited_time": TABLE_TIME,
                "created_by": {"object": "user", "id": user_id},
                "last_edited_by": {"object": "user", "id": user_id},
                "has_children": False,
                "in_trash": False,
                "type": "table_row",
                "table_row": {"cells": [[seg(c)] for c in cells]},
                "archived": False,
            }
        )
    return out


# ---- the customer pages ------------------------------------------------------


def _health_footer(page: Page, synced: str, sources: str = "HubSpot") -> Page:
    return (
        page.h2("Health Signal")
        .para("Account health is steady; no open escalations.")
        .para(f"Last synced: {synced} | Sources: {sources}")
    )


def customer_acme_robotics() -> list[dict[str, Any]]:
    p = Page("customer_acme_robotics", when="2026-03-18T09:05:00.000Z")
    p.callout(
        "🤖",
        "Acme Robotics builds warehouse automation. This page is maintained by an "
        "automated account-sync job; edits are overwritten.",
    )
    p.h2("Company Overview")
    p.field("Domain", "acmerobotics.example.test")
    p.field("Industry", "Industrial Automation")
    p.field("Employees", "1,200")
    p.field("HQ", "Springfield")
    p.field("Annual Revenue", "~$50M")
    p.field("Founded", "2009")
    p.field("Phone", "+1 555 0100")
    p.field("Parent Company", "Acme Holdings (separate CRM deal track)")
    p.field("HubSpot ID", "48201937465 | HubSpot Lifecycle: Customer [HubSpot]")
    p.h2("Account Relationship")
    p.field("ARR", "$250,000.00/yr — Source: account owner (2026-05-27)")
    p.h2("Usage Allocation")
    p.bullet("Base allocation: 40M units per quarter")
    p.bullet("Overage billed monthly at the list rate")
    p.field("Source", "Order form signed 2025-01-15")
    p.field("Deployment", "Self-Managed (primary) + SaaS components")
    p.field("Slack Channel", "#acme-robotics (C0000000001)")
    p.field("Status", "Active customer — expansion pipeline reduced; renewal due Q4")
    p.h2("Active Deals Summary")
    p.para("Two open deals as of the last sync.")
    p.bullet("Acme Robotics Expansion | $120,000 | Proposal | 2026-09-30")
    p.rich_bullet(
        [
            seg("Acme Robotics SaaS Add-on | $60,000 | "),
            seg("Closed Won", bold=True),
            seg(" | 2026-07-14"),
        ]
    )
    p.bullet("Acme Robotics Training Package | $15,000 | Negotiation | 2026-10-15")
    p.table(4)
    p.h2("Closed Deals (Historical)")
    p.table(4)
    p.h2("📊 Usage History (Jan 2025 – Feb 2026)")
    p.rich_para(
        [
            seg("Source: ", italic=True),
            seg(
                "Usage export (2026-04-07)",
                italic=True,
                link="https://docs.example.test/exports/usage-2026-04-07",
            ),
        ]
    )
    p.field("Q1 2025", "41K units, 190 calls")
    p.field("Q2 2025", "1.9M units, 5.2K calls")
    p.field("Q3 2025", "4.4M units, 7.1K calls")
    p.field("Total (14 months)", "309.6M units, 118.4K calls")
    p.h3("Key Signals")
    p.field("📈 Jan→Feb 2026", "usage up 64% month over month")
    p.bullet("Adoption is concentrated in the engineering group")
    p.field("Peak month", "Feb 2026 (96M units)")
    p.h2("💰 Cost & Margin Analysis")
    p.rich_para(
        [
            seg("Methodology: "),
            seg("$50 per 1M units", bold=True),
            seg(". Modeled from usage exports."),
        ]
    )
    p.h3("Margin Analysis")
    p.field("Current ARR", "$250,000")
    p.field("Margin", "-5.0%")
    p.field("Status", "🔴 NEGATIVE MARGIN. Losing about $12,500/yr at current usage.")
    p.h3("Assessment")
    p.para("Usage outpaces the contracted allocation; revisit pricing at renewal.")
    p.divider()
    p.callout(
        "📊", "Margin warning: usage growth is not matched by contract value.", "yellow_background"
    )
    _health_footer(p, "2026-07-29", "HubSpot, Slack")
    p.h2("Health Score Breakdown")
    p.field("Champion Strength", "8/10")
    p.field("Adoption Depth", "6/10")
    p.divider()
    p.h2("Key Contacts")
    p.para("[HubSpot — last synced 2026-05-21]")
    p.table(4)
    p.divider()
    p.para("Last synced: 2026-07-29 | Source: HubSpot | Maintained by: account-sync job")
    p.h2("Key Contacts (Acme Side)")
    p.bullet("Robin (Robin A / Robin B) - Primary technical lead. Owns the rollout")
    p.bullet("Quinn - Day-to-day technical contact")
    p.bullet("Morgan - Procurement contact")
    p.h2("Technical Profile")
    p.field("Deployment", "Self-Managed on AWS [Slack]")
    p.field("App URL", "https://app.acmerobotics.example.test")
    p.field("SSO", "SAML via the customer's identity provider")
    p.h2("Customer Priorities (as of Oct 2025)")
    p.numbered("Roll the platform out to the second plant")
    p.numbered("Reduce time to onboard new engineers")
    p.numbered("Document the change-approval workflow")
    p.h2("Support Commitments")
    p.field("Tier", "Tier 1 | Revenue: $150,000 | Open Requests: 1")
    p.h2("📋 Changelog")
    p.bullet("2026-07-29: refreshed contacts and deal summary")
    p.callout("📋", "2026-05-27: ARR confirmed by the account owner.")
    p.callout("📁", "2026-03-18: page created from the account template.")
    return p.blocks


def poc_globex_labs() -> list[dict[str, Any]]:
    p = Page("poc_globex_labs", when="2026-06-02T13:22:00.000Z")
    p.h2("Company Overview [HubSpot]")
    p.field("Domain", "globexlabs.example.test")
    p.field("Industry", "INFORMATION_TECHNOLOGY_AND_SERVICES")
    p.field("Employees", "90,000")
    p.field("Location", "Gotham")
    p.field("Annual Revenue", "$2,400,000,000")
    p.field("Founded", "1976")
    p.h2("Account Relationship")
    p.field("Status", "🟢 Active POC (confirmed 2026-04-09)")
    p.field("Tier", "None | Open Requests: 4")
    p.h2("Active Deals [HubSpot]")
    p.bullet("Globex Labs Pilot | $40,000 | Qualified | 2026-11-30")
    p.h2("Key Contacts [HubSpot]")
    p.bullet(
        "Ines Calderwood - Director Consulting Expert (ines.calderwood@globexlabs.example.test)"
    )
    p.bullet("Sam Whitlock - Partner, CIO (s.whitlock@globexlabs.example.test)")
    p.bullet("Bo Hargreave - Cybersecurity Leader (bo.hargreave@globexlabs.example.test)")
    p.bullet("Bo Hargreave - Business Continuity Director (bohargreave@mail.example.test)")
    p.bullet("Rue Okafor - VP, CTO (rue.okafor@globexlabs.example.test)")
    _health_footer(p, "2026-07-03 09:18 UTC")
    p.divider()
    p.h3("📋 Changelog — 2026-06-02")
    p.bullet("Updated contacts from the CRM export")
    p.para("Last synced: 2026-06-02 13:22 UTC")
    p.callout("📋", "2026-06-02: contacts refreshed.")
    p.divider()
    return p.blocks


def prospect_initrode() -> list[dict[str, Any]]:
    p = Page("prospect_initrode", when="2026-03-19T08:30:00.000Z")
    p.h2("Company Overview [HubSpot]")
    p.field("Domain", "initrode.example.test")
    p.field("Industry", "HUMAN_RESOURCES")
    p.field("Employees", "350")
    p.field("Location", "Pawnee")
    p.field("Annual Revenue", "$50,000,000")
    p.field("Founded", "1998")
    p.h2("Account Relationship")
    p.field("Status", "🟢 Customer")
    p.h2("Active Deals [HubSpot]")
    p.bullet("Initrode Platform Evaluation | $30,000 | Discovery | 2026-12-01")
    p.bullet("Initrode Analytics Seats | $18,000 | Discovery | 2027-01-15")
    p.bullet("Initrode Security Review | $9,500 | Discovery | 2027-02-01")
    p.h2("Key Contacts [HubSpot]")
    p.bullet("Mara Foster - Chief Information Officer (mfoster@initrode.example.test)")
    p.bullet("Jo Okabe - CIO (jo.okabe@initrode.example.test)")
    p.bullet("Clark Hallam - Treasury Manager (challam@initrode.example.test)")
    p.bullet("Jen Bucks - Software Engineer (jen.bucks@initrode.example.test)")
    _health_footer(p, "2026-03-19")
    return p.blocks


def churned_soylent_metrics() -> list[dict[str, Any]]:
    p = Page("churned_soylent_metrics", when="2026-03-19T08:47:00.000Z")
    p.h2("Company Overview [HubSpot]")
    p.field("Domain", "soylentmetrics.example.test")
    p.field("Industry", "INFORMATION_TECHNOLOGY_AND_SERVICES")
    p.field("Annual Revenue", "$10,000,000")
    p.h2("Account Relationship")
    p.field("ARR", "$24,000.00/yr ($2,000.00/mo) — Source: account owner (2026-05-27)")
    p.h2("Usage Allocation")
    p.field("Status", "🟢 Customer")
    p.h2("Active Deals [HubSpot]")
    p.bullet("No open deals")
    p.h2("Key Contacts [HubSpot]")
    p.bullet("Alex Cornell - Chief Technology Officer (alexc@soylentmetrics.example.test)")
    p.bullet("Tim Sayer - Managing Partner (tim@soylentmetrics.example.test)")
    p.bullet("Alexa L - Customer Service Representative (alexal@soylentmetrics.example.test)")
    p.bullet("Yara None (yara@soylentmetrics.example.test)")
    p.h2("📊 Usage History (Jan 2025 – Feb 2026)")
    p.field("Q1 2025", "7K units, 22 calls")
    p.field("Q2 2025", "350K units, 900 calls")
    p.h3("Key Signals")
    p.field("Peak month", "Mar 2025 (180K units)")
    p.h2("💰 Cost & Margin Analysis")
    p.rich_para(
        [
            seg("Methodology: "),
            seg("$50 per 1M units", bold=True),
            seg(". Modeled from usage exports."),
        ]
    )
    p.h3("Margin Analysis")
    p.field("Status", "✅ Above target. Current ARR ($24,000) exceeds minimum ($3,000).")
    p.h3("Assessment")
    p.para("Healthy margin while active; the account has since churned.")
    _health_footer(p, "2026-03-19")
    p.h2("Next Best Actions")
    p.para("Last synced: 2026-03-19 | Sources: HubSpot")
    p.divider()
    p.child_page("Usage Analysis — March to June 2026")
    return p.blocks


def partner_vandelay_cyber() -> list[dict[str, Any]]:
    """A prose-heavy page with few ``Label: value`` lines."""
    p = Page("partner_vandelay_cyber", when="2026-03-19T08:30:00.000Z")
    p.h2("Company Overview [HubSpot]")
    p.field("Domain", "vandelaycyber.example.test")
    p.field("Industry", "COMPUTER_SOFTWARE")
    p.h2("Account Relationship")
    p.field("Status", "🟢 Customer")
    p.h2("Active Deals [HubSpot]")
    p.bullet("Reseller agreement under review")
    p.h2("Key Contacts [HubSpot]")
    p.bullet("Jess Duarte - Chief Growth Officer (jess@vandelaycyber.example.test)")
    p.bullet("Tom Rhodes - Technical Lead (tom@vandelaycyber.example.test)")
    p.bullet("Joan Gasper - Quantum Entanglement Officer (joan.gasper@vandelaycyber.example.test)")
    p.bullet("Jay Byrd - Chief Executive Officer (jay.byrd@vandelaycyber.example.test)")
    p.bullet("Pat Clayton - CISO (pat.clayton@acmerobotics.example.test)")
    _health_footer(p, "2026-03-26 17:05 UTC")
    p.h2("Changelog")
    p.h2("Changelog")
    return p.blocks


def partner_table_rows() -> list[dict[str, Any]]:
    return table_rows(
        "partner_table_rows",
        [
            ["Account", "Contact", "Reason Kept Active", "Flagged By", "Status"],
            [
                "Northwind Partners",
                "Ada Fenwick",
                "Early design partner",
                "Partnerships",
                "Active — VAR",
            ],
            ["Pied Piper Consulting", "Devin Mora", "Early design partner", "Sales", "Active"],
            ["Tyrell Advisory", "—", "Resells to two accounts", "Sales", "Active"],
            ["Cyberdyne Resellers", "Jules Brandt", "Still evaluating", "Support", "Active"],
            ["Massive Dynamic Group", "—", "1 user", "—", "Active"],
        ],
    )


def linear_customers_response() -> dict[str, Any]:
    """The GraphQL ``customers`` page for ``linear_client._CUSTOMER_FIELDS``:
    ``id name domains revenue externalIds updatedAt status{id name} tier{id name}``.
    """
    ids = Ids("linear_customers_response")
    status = {name: {"id": ids.next(), "name": name} for name in ("Active", "Prospect", "Churned")}
    tier = {"id": ids.next(), "name": "Tier 1"}
    nodes = [
        {
            "id": ids.next(),
            "name": "Acme Robotics",
            "domains": [],
            "revenue": None,
            "externalIds": [],
            "updatedAt": "2026-08-12T08:11:45.318Z",
            "status": status["Active"],
            "tier": None,
        },
        {
            "id": ids.next(),
            "name": "Globex Labs",
            "domains": ["globexlabs.example.test"],
            "revenue": None,
            "externalIds": [],
            "updatedAt": "2026-09-03T14:26:09.742Z",
            "status": status["Active"],
            "tier": None,
        },
        {
            "id": ids.next(),
            "name": "Initrode Systems",
            "domains": ["initrode.example.test"],
            "revenue": None,
            "externalIds": [],
            "updatedAt": "2026-06-22T11:05:31.087Z",
            "status": status["Prospect"],
            "tier": None,
        },
        {
            "id": ids.next(),
            "name": "Soylent Metrics",
            "domains": ["soylentmetrics.example.test"],
            "revenue": 24000,
            "externalIds": ["soylent-metrics"],
            "updatedAt": "2026-09-17T20:48:56.653Z",
            "status": status["Active"],
            "tier": tier,
        },
    ]
    return {
        "data": {
            "customers": {
                "pageInfo": {"hasNextPage": True, "endCursor": nodes[-1]["id"]},
                "nodes": nodes,
            }
        }
    }


FIXTURES = {
    "customer_acme_robotics": customer_acme_robotics,
    "poc_globex_labs": poc_globex_labs,
    "prospect_initrode": prospect_initrode,
    "churned_soylent_metrics": churned_soylent_metrics,
    "partner_vandelay_cyber": partner_vandelay_cyber,
    "partner_table_rows": partner_table_rows,
    "linear_customers_response": linear_customers_response,
}


def render(name: str) -> str:
    return json.dumps(FIXTURES[name](), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out_dir = Path(args[0]) if args else Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in FIXTURES:
        (out_dir / f"{name}.json").write_text(render(name))
        print(f"wrote {out_dir / (name + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
