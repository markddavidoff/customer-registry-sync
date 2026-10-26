from pathlib import Path

from customer_registry_sync.datasets import (
    DATASETS_FILE,
    DEFAULT_STATUS_TABLE,
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    dataset_dir,
    datasets_errors,
    load_datasets,
    load_datasets_config,
)
from customer_registry_sync.manifest import MANIFEST, write_manifest
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import dataset_files, write_dataset

PACKAGE = Path(__file__).resolve().parents[1]


def _rec(name: str, domain: str) -> CustomerRecord:
    return CustomerRecord(canonical_name=name, domains=[domain], classification="real")


def test_config_round_trip_and_default_when_absent(tmp_path):
    assert load_datasets_config(tmp_path) == DatasetConfig(
        names=[NOTION], status_classification=DEFAULT_STATUS_TABLE
    )
    (tmp_path / DATASETS_FILE).write_text(
        "datasets: [notion, linear-found]\n"
        "linear_status_classification:\n"
        "  Active: real\n"
        "  Prospect: prospect\n"
        "  Churned: churned\n"
        "  Paused: churned\n"
    )
    cfg = load_datasets_config(tmp_path)
    assert cfg.names == [NOTION, LINEAR_FOUND]
    assert cfg.status_classification == {**DEFAULT_STATUS_TABLE, "Paused": "churned"}
    assert dataset_dir(tmp_path, LINEAR_FOUND) == tmp_path / "datasets" / "linear-found"


def test_errors_unlisted_dir_missing_dir_duplicate_and_empty(tmp_path):
    write_dataset([_rec("Acme", "acme.com")], tmp_path)
    assert datasets_errors(tmp_path) == []  # default config matches the one directory

    (tmp_path / "datasets" / "intercom").mkdir()
    assert datasets_errors(tmp_path) == ["datasets.yaml: directory datasets/intercom is not listed"]

    (tmp_path / DATASETS_FILE).write_text("datasets: [notion, intercom, linear-found]\n")
    assert datasets_errors(tmp_path) == ["datasets.yaml: linear-found has no directory"]

    (tmp_path / DATASETS_FILE).write_text("datasets: [notion, notion, intercom]\n")
    assert datasets_errors(tmp_path) == ["datasets.yaml: notion listed twice"]

    (tmp_path / DATASETS_FILE).write_text("datasets: []\n")
    assert "datasets.yaml: empty datasets list" in datasets_errors(tmp_path)

    (tmp_path / DATASETS_FILE).write_text(
        "datasets: [notion, intercom]\nlinear_status_classification:\n  Active: gold\n"
    )
    assert datasets_errors(tmp_path) == [
        "datasets.yaml: status Active maps to invalid classification gold"
    ]


def test_load_datasets_priority_order(tmp_path):
    yh = [_rec("Beta", "beta.com"), _rec("Acme", "acme.com")]
    found = [_rec("Zed", "zed.com"), _rec("Aaa", "aaa.com")]
    write_dataset(yh, tmp_path)
    write_dataset(found, tmp_path, dataset=LINEAR_FOUND)
    for name in (NOTION, LINEAR_FOUND):
        write_manifest(dataset_dir(tmp_path, name), "ingest")
    (tmp_path / DATASETS_FILE).write_text("datasets: [linear-found, notion]\n")
    # the config's order, not the alphabet or the directory listing, decides
    loaded = load_datasets(tmp_path, load_datasets_config(tmp_path))
    assert [(d.dataset, d.source_id) for d in loaded] == [
        (LINEAR_FOUND, "aaa"),
        (LINEAR_FOUND, "zed"),
        (NOTION, "acme"),
        (NOTION, "beta"),
    ]

    (tmp_path / DATASETS_FILE).write_text("datasets: [notion, linear-found]\n")
    loaded = load_datasets(tmp_path, load_datasets_config(tmp_path))
    assert [d.dataset for d in loaded] == [NOTION] * 2 + [LINEAR_FOUND] * 2
    # source_id is the file stem write_dataset chose; the manifest is skipped
    expected = [stem for stem, _ in dataset_files(yh)]
    assert [d.source_id for d in loaded[:2]] == sorted(expected)
    assert {d.record.canonical_name for d in loaded[:2]} == {"Acme", "Beta"}
    assert all(d.source_id != MANIFEST.removesuffix(".yaml") for d in loaded)


def test_no_old_dataset_path_remains():
    old_dir = "customers/" + "dataset/"
    old_join = "/ " + '"dataset"'
    roots = [PACKAGE / "src", PACKAGE / "tests"]
    offenders = [
        str(path)
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yml", ".yaml"}
        and any(needle in path.read_text() for needle in (old_dir, old_join))
    ]
    assert offenders == []
