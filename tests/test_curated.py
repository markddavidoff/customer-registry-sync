import yaml

from customer_registry_sync.curated import (
    CURATED_INPUTS,
    RETIRE_FILE,
    CuratedFields,
    load_curated,
    write_curated,
)
from customer_registry_sync.datasets import DATASETS_FILE
from customer_registry_sync.identity import IDENTITIES_FILE
from customer_registry_sync.ignore import IGNORE_FILE

CURATED_FILE = RETIRE_FILE  # the one curated file of this shape left (enrichment is gone)


def test_round_trip_omits_empty_fields(tmp_path):
    path = tmp_path / CURATED_FILE
    entries = {
        "zeta": CuratedFields(domains=["z.com"], revenue=500, tier="Tier 1"),
        "alpha": CuratedFields(revenue=100),
        "empty": CuratedFields(),
    }
    write_curated(entries, path, header="hand-curated")
    raw = yaml.safe_load(path.read_text())
    assert list(raw) == ["alpha", "zeta"]  # sorted, empty entry dropped
    assert raw["alpha"] == {"revenue": 100}  # empty fields omitted
    assert raw["zeta"] == {"domains": ["z.com"], "revenue": 500, "tier": "Tier 1"}
    assert load_curated(path) == {k: v for k, v in entries.items() if not v.empty()}


def test_missing_file_is_empty(tmp_path):
    assert load_curated(tmp_path / RETIRE_FILE) == {}


def test_header_written_as_comment(tmp_path):
    path = tmp_path / CURATED_FILE
    write_curated({"a": CuratedFields(tier="T")}, path, header="line one\nline two")
    lines = path.read_text().splitlines()
    assert lines[0] == "# line one"
    assert lines[1] == "# line two"
    assert yaml.safe_load(path.read_text()) == {"a": {"tier": "T"}}


def test_curated_inputs_name_the_files_check_and_deploy_read():
    # Names relative to the registry's data_dir; no module reads a hard-coded
    # `customers/` path. Each constant is the one the loading module uses.
    assert CURATED_INPUTS == (IDENTITIES_FILE, IGNORE_FILE, DATASETS_FILE, RETIRE_FILE)
