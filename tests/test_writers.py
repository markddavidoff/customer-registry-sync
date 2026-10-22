import yaml

from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    load_desired,
    load_state,
    slug_for,
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


def test_writes_one_yaml_per_customer_and_two_csvs(tmp_path):
    write_dataset(sample(), tmp_path)
    ds = tmp_path / "dataset"
    assert (ds / "acmerobotics.yaml").exists()
    assert (tmp_path / "registry.csv").exists()
    assert (tmp_path / "real-customers.csv").exists()
    assert len(list(ds.glob("*.yaml"))) == 4


def test_real_customers_csv_only_real_and_partner(tmp_path):
    write_dataset(sample(), tmp_path)
    text = (tmp_path / "real-customers.csv").read_text()
    assert "Acme Robotics" in text and "Pied Piper Consulting" in text  # real + partner included
    assert (
        "Initrode Systems" not in text and "Soylent Metrics" not in text
    )  # prospect + churned excluded


def test_idempotent_identical_bytes_on_rerun(tmp_path):
    write_dataset(sample(), tmp_path)
    reg = (tmp_path / "registry.csv").read_bytes()
    yml = (tmp_path / "dataset" / "acmerobotics.yaml").read_bytes()
    write_dataset(sample(), tmp_path)
    assert (tmp_path / "registry.csv").read_bytes() == reg
    assert (tmp_path / "dataset" / "acmerobotics.yaml").read_bytes() == yml


def test_stale_dataset_yaml_removed_on_rerun(tmp_path):
    write_dataset(sample(), tmp_path)
    assert (tmp_path / "dataset" / "soylent-metrics.yaml").exists()
    write_dataset([r for r in sample() if r.canonical_name != "Soylent Metrics"], tmp_path)
    assert not (tmp_path / "dataset" / "soylent-metrics.yaml").exists()


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
