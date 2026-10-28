"""Every curated example file loads cleanly through the loader that reads it."""

import shutil
from pathlib import Path

from gitops_engine.datasets import dataset_dir
from gitops_engine.ignore import BUILTIN_PREFIX, ignore_errors, load_ignore
from gitops_engine.retire import load_retire

from customer_registry_sync.config import load_config
from customer_registry_sync.datasets import load_datasets, load_datasets_config
from customer_registry_sync.identity import load_identities
from customer_registry_sync.linear_files import IGNORE_FORMAT
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.inputs import load_overrides, load_self_managed_customers
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


def test_ignore_example_loads_without_errors():
    path = EXAMPLES / "linear-ignore.example.yaml"
    assert ignore_errors(path, IGNORE_FORMAT) == []
    ignore = load_ignore(path, IGNORE_FORMAT)
    assert [e.live_id for e in ignore.entries] == ["00000000-0000-4000-8000-0000000000aa"]
    assert {p.prefix for p in ignore.prefixes} == {BUILTIN_PREFIX, "Sandbox-"}


def test_retire_example_is_comments_only():
    assert load_retire(EXAMPLES / "linear-retire.example.yaml", LINEAR_SCHEMA) == {}


def test_input_examples_load():
    assert load_overrides(EXAMPLES / "classification-overrides.example.yaml") == {}
    customers = load_self_managed_customers(EXAMPLES / "self-managed-customers.example.yaml")
    assert [c.canonical_name for c in customers] == [
        "Stark Fabrication LLC (The Forge)",
        "Tyrell Corporation",
    ]
