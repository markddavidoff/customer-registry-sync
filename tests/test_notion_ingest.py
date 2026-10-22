import json
from pathlib import Path

from customer_registry_sync.notion_ingest import (
    _is_customer_page,
    classification_for_bucket,
    norm_arr,
    norm_deployment,
    parse_customer_page,
    parse_partner_table,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return json.loads((FIX / f"{name}.json").read_text())


def test_classification_for_bucket():
    assert classification_for_bucket("Customers") == "real"
    assert classification_for_bucket("Churned Customers") == "churned"
    assert classification_for_bucket("Archive") == "internal"
    assert classification_for_bucket("POCs") == "prospect"
    assert classification_for_bucket("Prospects") == "prospect"
    assert classification_for_bucket("Partner & Supporting Accounts") == "partner"


def test_norm_arr_pulls_first_dollar_amount():
    assert norm_arr("$250,000.00/yr — Source: account owner (2026-05-27)") == 250000.0
    assert norm_arr("TBD") is None
    assert norm_arr("") is None


def test_norm_arr_handles_k_m_b_shorthand():
    assert norm_arr("$500K") == 500_000.0
    assert norm_arr("$1.2M") == 1_200_000.0
    assert norm_arr("$2B/yr") == 2_000_000_000.0
    assert norm_arr("$250,000.00/yr") == 250_000.0  # trailing /yr is not a suffix


def test_norm_deployment():
    assert norm_deployment("Self-Managed (primary) + SaaS components") == "hybrid"
    assert norm_deployment("Self-Managed") == "on-prem"
    assert norm_deployment("SaaS") == "saas"
    assert norm_deployment("on-prem / air-gapped") == "on-prem"
    assert norm_deployment("unknown") is None


def test_parse_customer_page_acme_rich_fields():
    rec = parse_customer_page(
        load("customer_acme_robotics"), bucket="Customers", page_id="acme-id", title="Acme Robotics"
    )
    assert rec.name == "Acme Robotics"
    assert rec.classification == "real"
    assert rec.domains == ["acmerobotics.example.test"]
    assert rec.arr == 250000.0  # the ARR line, not the ~$50M company revenue
    assert rec.deployment == "hybrid"  # "Self-Managed (primary) + SaaS components"
    assert rec.hubspot_id == "48201937465"
    assert rec.status and "Active" in rec.status
    assert rec.source_page_id == "acme-id"


def test_tier_is_cleaned_to_first_segment():
    acme = parse_customer_page(
        load("customer_acme_robotics"), bucket="Customers", page_id="a", title="Acme Robotics"
    )
    assert acme.tier == "Tier 1"  # not the full "Tier 1 | Revenue: … | Open …"
    globex = parse_customer_page(
        load("poc_globex_labs"), bucket="POCs", page_id="g", title="Globex Labs (US)"
    )
    assert globex.tier is None  # "Tier: None | …" → null


def test_contact_skips_hubspot_annotations():
    acme = parse_customer_page(
        load("customer_acme_robotics"), bucket="Customers", page_id="a", title="Acme Robotics"
    )
    assert acme.contact is not None
    assert not acme.contact.startswith("[")  # never the "[HubSpot — last synced]" line
    assert "last synced" not in acme.contact.lower()
    assert acme.contact == "Robin"  # first real named contact


def test_parse_poc_page_leaves_unknown_fields_null():
    rec = parse_customer_page(
        load("poc_globex_labs"), bucket="POCs", page_id="globex-id", title="Globex Labs (US)"
    )
    assert rec.name == "Globex Labs (US)"
    assert rec.classification == "prospect"
    assert rec.domains == ["globexlabs.example.test"]
    assert rec.deployment is None  # POC pages carry no Deployment line
    assert rec.arr is None  # no ARR line → stays null (never invented)


def test_parse_prospect_does_not_mistake_company_revenue_for_arr():
    rec = parse_customer_page(
        load("prospect_initrode"),
        bucket="Prospects",
        page_id="initrode-id",
        title="Initrode Systems",
    )
    assert rec.classification == "prospect"
    assert rec.domains == ["initrode.example.test"]
    assert rec.arr is None  # "Annual Revenue: $50,000,000" is HubSpot co. revenue, not ARR
    assert rec.contact == "mfoster@initrode.example.test"  # first Key Contacts email


def test_parse_churned_page():
    rec = parse_customer_page(
        load("churned_soylent_metrics"),
        bucket="Churned Customers",
        page_id="soylent-id",
        title="Soylent Metrics",
    )
    assert rec.name == "Soylent Metrics"
    assert rec.classification == "churned"


def test_parse_prose_heavy_partner_page_does_not_crash():
    rec = parse_customer_page(
        load("partner_vandelay_cyber"),
        bucket="Partner & Supporting Accounts",
        page_id="v-id",
        title="Vandelay Cyber",
    )
    assert rec.name == "Vandelay Cyber"
    assert rec.classification == "partner"


def test_blank_and_methodology_pages_are_skipped():
    # bucket pages can hold empty-title child pages and reference pages
    assert _is_customer_page("") is False
    assert _is_customer_page("   ") is False
    assert _is_customer_page("Start Here: Customer Methodology") is False
    assert _is_customer_page("Customer Health Reports") is False
    assert _is_customer_page("Archive") is False
    assert _is_customer_page("Churned Customers") is False
    assert _is_customer_page("Acme Robotics") is True


def test_parse_partner_table():
    recs = parse_partner_table(load("partner_table_rows"), bucket="Partner & Supporting Accounts")
    names = [r.name for r in recs]
    assert names == [
        "Northwind Partners",
        "Pied Piper Consulting",
        "Tyrell Advisory",
        "Cyberdyne Resellers",
        "Massive Dynamic Group",
    ]
    assert all(r.classification == "partner" for r in recs)
    northwind = recs[0]
    assert northwind.contact == "Ada Fenwick"
    assert northwind.status == "Active — VAR"
    tyrell = recs[2]
    assert tyrell.contact is None  # table cell "—" → null
