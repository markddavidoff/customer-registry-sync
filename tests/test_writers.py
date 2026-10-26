import csv
import io

import yaml

from customer_registry_sync.datasets import (
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    dataset_dir,
    load_datasets,
)
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    CSV_COLUMNS,
    csv_text,
    load_desired,
    load_state,
    slug_for,
    write_csvs,
    write_dataset,
    write_desired,
    write_state,
)


def sample() -> list[CustomerRecord]:
    return [
        CustomerRecord(
            canonical_name="Acme Robotics",
            domains=["acmerobotics.example.test"],
            buckets=["Customers"],
            classification="real",
            deployment="hybrid",
            arr=250000.0,
        ),
        CustomerRecord(
            canonical_name="Initrode Systems",
            domains=["initrode.example.test"],
            buckets=["Prospects"],
            classification="prospect",
        ),
        CustomerRecord(
            canonical_name="Pied Piper Consulting",
            buckets=["Partner & Supporting Accounts"],
            classification="partner",
        ),
        CustomerRecord(
            canonical_name="Soylent Metrics",
            buckets=["Churned Customers"],
            classification="churned",
        ),
    ]


def _entries(tmp_path, yh=(), found=()):
    """DatasetRecords as `generate` reads them: written by `write_dataset`, loaded back."""
    if yh:
        write_dataset(list(yh), tmp_path)
    if found:
        write_dataset(list(found), tmp_path, dataset=LINEAR_FOUND)
    return load_datasets(tmp_path, DatasetConfig(names=[NOTION, LINEAR_FOUND]))


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_writes_one_yaml_per_customer_and_no_csvs(tmp_path):
    write_dataset(sample(), tmp_path)
    ds = dataset_dir(tmp_path, NOTION)
    assert (ds / "acmerobotics.yaml").exists()
    assert not (tmp_path / "registry.csv").exists()  # CSVs come from generate only
    assert not (tmp_path / "real-customers.csv").exists()
    assert len(list(ds.glob("*.yaml"))) == 4


def test_write_csvs_writes_both_csvs(tmp_path):
    write_csvs(_entries(tmp_path, yh=sample()), tmp_path)
    names = [r["canonical_name"] for r in _rows((tmp_path / "registry.csv").read_text())]
    assert names == [
        "Acme Robotics",
        "Initrode Systems",
        "Pied Piper Consulting",
        "Soylent Metrics",
    ]  # sorted by lower-cased name
    assert (tmp_path / "real-customers.csv").exists()


def test_real_customers_csv_only_real_and_partner(tmp_path):
    write_csvs(_entries(tmp_path, yh=sample()), tmp_path)
    text = (tmp_path / "real-customers.csv").read_text()
    assert "Acme Robotics" in text and "Pied Piper Consulting" in text  # real + partner included
    assert (
        "Initrode Systems" not in text and "Soylent Metrics" not in text
    )  # prospect + churned excluded


def test_idempotent_identical_bytes_on_rerun(tmp_path):
    write_dataset(sample(), tmp_path)
    write_csvs(_entries(tmp_path), tmp_path)
    reg = (tmp_path / "registry.csv").read_bytes()
    yml = (dataset_dir(tmp_path, NOTION) / "acmerobotics.yaml").read_bytes()
    write_dataset(sample(), tmp_path)
    write_csvs(_entries(tmp_path), tmp_path)
    assert (tmp_path / "registry.csv").read_bytes() == reg
    assert (dataset_dir(tmp_path, NOTION) / "acmerobotics.yaml").read_bytes() == yml


def test_csv_text_matches_written_bytes(tmp_path):
    entries = _entries(tmp_path, yh=sample())
    write_csvs(entries, tmp_path)
    assert (tmp_path / "registry.csv").read_bytes() == csv_text(entries).encode()


def test_csv_has_dataset_column_last(tmp_path):
    stub = CustomerRecord(
        canonical_name="Umbrella Analytics",
        domains=["umbrella.example.test"],
        classification="prospect",
        linear_customer_id="lin_1",
        sources=["linear:lin_1"],
    )
    text = csv_text(_entries(tmp_path, yh=sample(), found=[stub]))
    assert CSV_COLUMNS[-1] == "dataset"
    assert text.splitlines()[0].split(",")[-1] == "dataset"
    by_name = {r["canonical_name"]: r["dataset"] for r in _rows(text)}
    assert by_name["Acme Robotics"] == NOTION and by_name["Umbrella Analytics"] == LINEAR_FOUND


def test_real_customers_includes_real_stubs(tmp_path):
    real_stub = CustomerRecord(
        canonical_name="Umbrella Analytics",
        domains=["umbrella.example.test"],
        classification="real",
        linear_customer_id="lin_1",
        sources=["linear:lin_1"],
    )
    prospect_stub = CustomerRecord(
        canonical_name="Globex Corp",
        domains=["globex.example.test"],
        classification="prospect",
        linear_customer_id="lin_2",
    )
    write_csvs(_entries(tmp_path, yh=sample(), found=[real_stub, prospect_stub]), tmp_path)
    rows = {r["canonical_name"]: r for r in _rows((tmp_path / "real-customers.csv").read_text())}
    assert rows["Umbrella Analytics"]["dataset"] == LINEAR_FOUND
    assert "Globex Corp" not in rows


def test_linear_found_dataset_write_leaves_csvs_untouched(tmp_path):
    write_csvs(_entries(tmp_path, yh=sample()), tmp_path)
    before = {n: (tmp_path / n).read_bytes() for n in ("registry.csv", "real-customers.csv")}
    stub = CustomerRecord(
        canonical_name="Umbrella Analytics",
        domains=["umbrella.example.test"],
        classification="real",
        linear_customer_id="lin_1",
    )
    write_dataset([stub], tmp_path, dataset=LINEAR_FOUND)
    assert {n: (tmp_path / n).read_bytes() for n in before} == before


def test_stale_dataset_yaml_removed_on_rerun(tmp_path):
    write_dataset(sample(), tmp_path)
    assert (dataset_dir(tmp_path, NOTION) / "soylent-metrics.yaml").exists()
    write_dataset([r for r in sample() if r.canonical_name != "Soylent Metrics"], tmp_path)
    assert not (dataset_dir(tmp_path, NOTION) / "soylent-metrics.yaml").exists()


def test_writer_does_not_touch_curated_input_files(tmp_path):
    self_managed = tmp_path / "self-managed-customers.yaml"
    self_managed.write_text("customers: []\n# hand edit preserved\n")
    before = self_managed.read_bytes()
    write_dataset(sample(), tmp_path)
    assert self_managed.read_bytes() == before


def test_slug_from_domain_then_name():
    assert (
        slug_for(
            CustomerRecord(canonical_name="Acme Robotics", domains=["acmerobotics.example.test"])
        )
        == "acmerobotics"
    )
    assert (
        slug_for(CustomerRecord(canonical_name="Stark Fabrication LLC (The Forge)"))
        == "stark-fabrication-llc-the-forge"
    )


def _lc(name, xid, synced=True):
    rec = LinearCustomer(
        name=name, domains=[f"{xid}.com"], revenue=5, status="Active", external_ids=[xid]
    )
    if synced:
        rec.linear_synced = LinearSynced(
            "lin_" + xid, "2026-10-01T00:00:00.000Z", content_hash_of(rec)
        )
    return rec


def test_write_desired_strips_linear_synced_and_round_trips(tmp_path):
    write_desired([_lc("Acme", "acme")], tmp_path)
    data = yaml.safe_load((tmp_path / "linear" / "acme.yaml").read_text())
    assert "linear_synced" not in data
    [back] = load_desired(tmp_path)
    assert back.name == "Acme" and back.linear_synced is None


def test_write_state_keeps_linear_synced_in_linear_state_dir(tmp_path):
    write_state([_lc("Acme", "acme")], tmp_path)
    data = yaml.safe_load((tmp_path / "linear-state" / "acme.yaml").read_text())
    assert data["linear_synced"]["linear_id"] == "lin_acme"
    [back] = load_state(tmp_path)
    assert back.linear_synced.updated_at == "2026-10-01T00:00:00.000Z"
    assert not (tmp_path / "linear").exists()  # state never touches desired


def test_write_state_removes_stale_files(tmp_path):
    write_state([_lc("Acme", "acme"), _lc("Beta", "beta")], tmp_path)
    write_state([_lc("Acme", "acme")], tmp_path)
    assert sorted(p.name for p in (tmp_path / "linear-state").glob("*.yaml")) == ["acme.yaml"]


def test_write_state_dedups_duplicate_names(tmp_path):
    a = LinearCustomer(name="Dup", status="Active")
    b = LinearCustomer(name="Dup", status="Active", revenue=9)
    write_state([a, b], tmp_path)
    names = sorted(p.name for p in (tmp_path / "linear-state").glob("*.yaml"))
    assert names == ["dup-2.yaml", "dup.yaml"]


def test_write_dataset_stems_and_dataset_directory(tmp_path):
    recs = sample()
    stems = [f"id-{i}" for i in range(len(recs))]
    write_dataset(recs, tmp_path, dataset="linear-found", stems=stems)
    names = sorted(p.stem for p in dataset_dir(tmp_path, "linear-found").glob("*.yaml"))
    assert names == sorted(stems)
    assert not dataset_dir(tmp_path, NOTION).exists()
