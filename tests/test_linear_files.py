"""The Linear sync's curated files on the engine's loaders: names, retire adapter."""

import pytest
import yaml
from gitops_engine.datasets import DATASETS_FILE
from gitops_engine.retire import load_retire, write_retire

from customer_registry_sync.identity import IDENTITIES_FILE
from customer_registry_sync.linear_files import (
    CURATED_INPUTS,
    IGNORE_FILE,
    RETIRE_FILE,
    CuratedFields,
    load_retire_fields,
)
from customer_registry_sync.linear_schema import LINEAR_SCHEMA


def test_retire_round_trip_omits_empty_fields_in_the_v010_layout(tmp_path):
    path = tmp_path / RETIRE_FILE
    entries = {
        "zeta": {"domains": ["z.com"], "revenue": 500, "tier": "Tier 1"},
        "alpha": {"revenue": 100},
        "empty": {},
    }
    write_retire(entries, path, header="hand-curated")
    assert path.read_text() == (
        "# hand-curated\n"
        "alpha:\n  revenue: 100\n"
        "zeta:\n  domains:\n  - z.com\n  revenue: 500\n  tier: Tier 1\n"
    )
    raw = yaml.safe_load(path.read_text())
    assert list(raw) == ["alpha", "zeta"]  # sorted, empty entry dropped
    assert load_retire(path, LINEAR_SCHEMA)["zeta"] == {
        "domains": ["z.com"],
        "revenue": 500,
        "tier": "Tier 1",
    }


def test_retire_fields_adapter_matches_the_v010_loader(tmp_path):
    path = tmp_path / RETIRE_FILE
    assert load_retire_fields(path) == {}  # a missing file
    path.write_text("zeta:\n  domains: [z.com]\n  revenue: 500\n  tier: Tier 1\nbare:\n")
    assert load_retire_fields(path) == {
        "zeta": CuratedFields(domains=["z.com"], revenue=500, tier="Tier 1"),
        "bare": CuratedFields(),
    }


@pytest.mark.parametrize("field", ["name", "status", "external_ids"])
def test_retire_file_rejects_an_always_field(tmp_path, field):
    path = tmp_path / RETIRE_FILE
    path.write_text(f"acme:\n  {field}: x\n")
    with pytest.raises(ValueError, match="cannot be retired"):
        load_retire_fields(path)


def test_curated_inputs_name_the_files_check_and_deploy_read():
    # Names relative to the registry's data_dir; no module reads a hard-coded
    # `customers/` path. Each constant is the one the loading module uses.
    assert CURATED_INPUTS == (IDENTITIES_FILE, IGNORE_FILE, DATASETS_FILE, RETIRE_FILE)
