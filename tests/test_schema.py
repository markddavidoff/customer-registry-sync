import pytest

from customer_registry_sync.schema import (
    CustomerRecord,
    RawCustomer,
    VALID_CLASSIFICATIONS,
    VALID_DEPLOYMENTS,
)


def test_customer_record_round_trips_through_dict():
    rec = CustomerRecord(
        canonical_name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        buckets=["Customers"],
        deployment="saas",
        classification="real",
        arr=250000.0,
        tier="Tier 1",
        status="Active",
        owner="Sam Rivera",
        contact="jane@acmerobotics.example.test",
        prod_org_id="cm0000000000000000000test1",
        hubspot_id="12345",
        linear_customer_id=None,
        sources=["notion:Customers"],
        notes="paying",
    )
    d = rec.to_dict()
    assert d["canonical_name"] == "Acme Robotics"
    assert d["domains"] == ["acmerobotics.example.test"]
    assert CustomerRecord.from_dict(d) == rec


def test_customer_record_requires_canonical_name():
    with pytest.raises((TypeError, ValueError)):
        CustomerRecord.from_dict({"domains": [], "buckets": []})


def test_customer_record_rejects_unknown_key():
    base = CustomerRecord(canonical_name="X").to_dict()
    base["surprise"] = "nope"
    with pytest.raises(ValueError, match="unknown"):
        CustomerRecord.from_dict(base)


def test_customer_record_rejects_bad_enum_values():
    with pytest.raises(ValueError, match="deployment"):
        CustomerRecord(canonical_name="X", deployment="cloud")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="classification"):
        CustomerRecord(canonical_name="X", classification="lead")  # type: ignore[arg-type]


def test_enum_sets_allow_null():
    assert None in VALID_DEPLOYMENTS
    assert None in VALID_CLASSIFICATIONS
    assert {"saas", "on-prem", "hybrid"} <= VALID_DEPLOYMENTS
    assert {"real", "prospect", "churned", "partner", "internal"} <= VALID_CLASSIFICATIONS


def test_raw_customer_carries_bucket_and_unknowns_stay_null():
    raw = RawCustomer(name="Globex Labs (US)", bucket="POCs")
    assert raw.bucket == "POCs"
    assert raw.deployment is None
    assert raw.arr is None
    assert raw.domains == []
