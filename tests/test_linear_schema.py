import pytest

from customer_registry_sync.linear_schema import (
    VALID_LINEAR_STATUSES,
    LinearCustomer,
    LinearSynced,
    content_hash_of,
)


def sample(**kw) -> LinearCustomer:
    base = dict(
        name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        revenue=250000,
        tier="Tier 1",
        status="Active",
        external_ids=["acmerobotics"],
    )
    base.update(kw)
    return LinearCustomer(**base)


def test_round_trips_through_dict():
    rec = sample(
        linear_synced=LinearSynced(
            linear_id="lin-1", updated_at="2026-10-07T01:00:00.000Z", content_hash="abc"
        )
    )
    d = rec.to_dict()
    assert d["name"] == "Acme Robotics"
    assert d["linear_synced"]["linear_id"] == "lin-1"
    assert LinearCustomer.from_dict(d) == rec


def test_linear_synced_optional():
    rec = sample()
    assert rec.linear_synced is None
    assert LinearCustomer.from_dict(rec.to_dict()).linear_synced is None


def test_status_accepts_workspace_statuses_and_rejects_empty():
    # the 3 deployable statuses are the canonical set generate emits...
    assert {"Active", "Prospect", "Churned"} <= VALID_LINEAR_STATUSES
    # ...but a mirror row may carry any workspace-defined status (At-Risk/POC/Lost)
    sample(status="At-Risk")
    sample(status="POC")
    with pytest.raises(ValueError, match="status"):
        sample(status="")


def test_content_hash_stable_across_calls():
    assert content_hash_of(sample()) == content_hash_of(sample())


def test_content_hash_ignores_linear_synced():
    a = sample(linear_synced=None)
    b = sample(linear_synced=LinearSynced("x", "y", "z"))
    assert content_hash_of(a) == content_hash_of(b)


def test_content_hash_changes_on_deployable_field():
    base = content_hash_of(sample())
    assert content_hash_of(sample(revenue=999999)) != base
    assert content_hash_of(sample(status="Churned")) != base
    assert content_hash_of(sample(domains=["other.com"])) != base
    assert content_hash_of(sample(external_ids=["x"])) != base


def test_content_hash_order_insensitive_for_lists():
    a = sample(domains=["a.com", "b.com"], external_ids=["x", "y"])
    b = sample(domains=["b.com", "a.com"], external_ids=["y", "x"])
    assert content_hash_of(a) == content_hash_of(b)
