"""Every curated example file loads cleanly through the loader that reads it."""

import shutil
from pathlib import Path

from gitops_engine.datasets import dataset_dir
from gitops_engine.ignore import BUILTIN_PREFIX
from gitops_engine.overrides import load_overrides as load_override_entries
from gitops_engine.overrides import override_errors

from customer_registry_sync.config import load_config
from customer_registry_sync.datasets import load_datasets, load_datasets_config
from customer_registry_sync.identity import load_identities
from customer_registry_sync.linear_overrides import OVERRIDES_FORMAT, value_type_failures
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.sources.notion import load_overrides, load_self_managed_customers
from customer_registry_sync.schema import VALID_CLASSIFICATIONS, CustomerRecord

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_registry_sync_example_loads():
    cfg = load_config(EXAMPLES / "registry-sync.example.yaml")
    assert cfg.workflow is not None


def test_identities_example_loads_and_uses_the_notion_source_key():
    table = load_identities(EXAMPLES / "identities.example.yaml")
    assert set(table.entries) == {"acme-robotics", "globex-labs"}
    assert table.entries["acme-robotics"].sources["notion"] == "acme-robotics"


def test_datasets_example_loads_in_priority_order(tmp_path):
    # The loader reads `datasets.yaml` from a data directory, so copy the example there.
    shutil.copy(EXAMPLES / "datasets.example.yaml", tmp_path / "datasets.yaml")
    cfg = load_datasets_config(tmp_path)
    assert cfg.names == ["notion", "linear-found"]
    assert set(cfg.status_classification.values()) <= VALID_CLASSIFICATIONS


def test_dataset_record_example_loads_through_the_dataset_reader(tmp_path):
    # `load_datasets` reads `datasets/<name>/<slug>.yaml`, so copy the example there.
    directory = dataset_dir(tmp_path, "notion")
    directory.mkdir(parents=True)
    shutil.copy(EXAMPLES / "dataset-record.example.yaml", directory / "acme-robotics.yaml")
    (entry,) = load_datasets(tmp_path, load_datasets_config(tmp_path))
    rec = entry.record
    assert (entry.dataset, entry.source_id) == ("notion", "acme-robotics")
    assert isinstance(rec, CustomerRecord)
    assert rec.canonical_name == "Acme Robotics"
    assert rec.hubspot_id == "48201937465"
    assert rec.prod_org_id is None and rec.owner is None
    assert rec.classification in VALID_CLASSIFICATIONS


def test_overrides_example_is_valid():
    path = EXAMPLES / "linear-overrides.example.yaml"
    assert override_errors(path, OVERRIDES_FORMAT, LINEAR_SCHEMA) == []
    ov = load_override_entries(path, OVERRIDES_FORMAT)
    assert value_type_failures(ov) == []
    entries = ov.entries
    # one of each entry kind in the format
    assert any(e.pin.get("revenue") is not None for e in entries.values())
    assert any("tier" in e.pin and e.pin["tier"] is None for e in entries.values())
    assert any(e.pin.get("name") for e in entries.values())
    assert any("domains" in e.add for e in entries.values())
    assert any("domains" in e.remove for e in entries.values())
    assert any("revenue" in e.pending for e in entries.values())
    assert any("domains_add" in e.pending for e in entries.values())
    assert any(e.hidden for e in entries.values())
    assert any(e.deleted is not None for e in entries.values())
    assert any(e.reason for e in entries.values())
    assert {p.prefix for p in ov.prefixes} == {BUILTIN_PREFIX, "Sandbox-"}


def test_removed_example_files_are_gone():
    assert not (EXAMPLES / "linear-ignore.example.yaml").exists()
    assert not (EXAMPLES / "linear-retire.example.yaml").exists()


def test_input_examples_load():
    assert load_overrides(EXAMPLES / "classification-overrides.example.yaml") == {}
    customers = load_self_managed_customers(EXAMPLES / "self-managed-customers.example.yaml")
    assert [c.canonical_name for c in customers] == [
        "Stark Fabrication LLC (The Forge)",
        "Tyrell Corporation",
    ]
