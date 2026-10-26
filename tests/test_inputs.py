from pathlib import Path

import pytest

from customer_registry_sync import inputs
from customer_registry_sync.inputs import load_overrides, load_self_managed_customers

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SELF_MANAGED_SEED = EXAMPLES / "self-managed-customers.example.yaml"
OVERRIDES_SEED = EXAMPLES / "classification-overrides.example.yaml"


def test_alias_input_is_gone():
    # Identity links now live in identities.yaml; there is no aliases file to load.
    assert not hasattr(inputs, "load_aliases")
    assert not (EXAMPLES / "linear-aliases.example.yaml").exists()


def test_self_managed_example_loads_and_has_on_prem_entries():
    recs = load_self_managed_customers(SELF_MANAGED_SEED)
    assert recs, "example should not be empty"
    by_name = {r.canonical_name: r for r in recs}
    assert "Stark Fabrication LLC (The Forge)" in by_name
    assert by_name["Stark Fabrication LLC (The Forge)"].deployment == "on-prem"
    assert all(r.deployment in ("on-prem", "hybrid") for r in recs)


def test_self_managed_defaults_classification_to_real(tmp_path):
    p = tmp_path / "self-managed.yaml"
    p.write_text("customers:\n  - name: Private Self-Managed Co\n    deployment: on-prem\n")
    rec = load_self_managed_customers(p)[0]
    assert rec.classification == "real"  # self-managed customers are paying customers by default


def test_self_managed_respects_explicit_classification(tmp_path):
    p = tmp_path / "self-managed.yaml"
    p.write_text(
        "customers:\n  - name: Foo\n    deployment: on-prem\n    classification: churned\n"
    )
    assert load_self_managed_customers(p)[0].classification == "churned"


def test_self_managed_rejects_bad_classification(tmp_path):
    p = tmp_path / "self-managed.yaml"
    p.write_text("customers:\n  - name: Foo\n    deployment: on-prem\n    classification: lead\n")
    with pytest.raises(ValueError, match="classification"):
        load_self_managed_customers(p)


def test_self_managed_rejects_bad_deployment(tmp_path):
    p = tmp_path / "self-managed.yaml"
    p.write_text("customers:\n  - name: Foo\n    deployment: cloud\n")
    with pytest.raises(ValueError, match="deployment"):
        load_self_managed_customers(p)


def test_self_managed_rejects_entry_without_name(tmp_path):
    p = tmp_path / "self-managed.yaml"
    p.write_text("customers:\n  - deployment: on-prem\n")
    with pytest.raises(ValueError, match="name"):
        load_self_managed_customers(p)


def test_overrides_example_loads_as_mapping():
    ov = load_overrides(OVERRIDES_SEED)
    assert isinstance(ov, dict)  # the example carries no active overrides → {}


def test_overrides_keyed_by_match(tmp_path):
    p = tmp_path / "ov.yaml"
    p.write_text(
        "overrides:\n"
        "  - match: demo.example.com\n"
        "    classification: internal\n"
        "    reason: internal demo org\n"
    )
    ov = load_overrides(p)
    assert ov["demo.example.com"]["classification"] == "internal"
    assert "reason" in ov["demo.example.com"]


def test_overrides_rejects_bad_classification(tmp_path):
    p = tmp_path / "ov.yaml"
    p.write_text("overrides:\n  - match: x.com\n    classification: lead\n")
    with pytest.raises(ValueError, match="classification"):
        load_overrides(p)
