"""Source adapters: the registry, the Notion adapter's config and its ingest."""

from pathlib import Path

import pytest
import yaml

from customer_registry_sync import cli
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.sources import SOURCES, SourceConfigError, SourceError
from customer_registry_sync.sources.notion import NotionSource

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _section(buckets: dict[str, str]) -> dict:
    return {"kind": "notion", "hub_page_id": "hub", "buckets": buckets}


def test_registry_maps_names_to_adapters():
    assert SOURCES == {"notion": NotionSource}
    assert all(cls.name == key for key, cls in SOURCES.items())


def test_from_config_reads_hub_and_buckets(fake_notion):
    src = NotionSource.from_config(_section(fake_notion))
    assert src.hub_page_id == "hub"
    assert src.buckets == fake_notion


def test_from_config_rejects_a_bucket_name_the_template_lacks(fake_notion):
    with pytest.raises(SourceConfigError, match="Leads"):
        NotionSource.from_config(_section({**fake_notion, "Leads": "x"}))


@pytest.mark.parametrize("bad", ["a string", ["a"], None])
def test_from_config_requires_a_buckets_mapping(bad):
    with pytest.raises(SourceConfigError, match=r"source\.buckets"):
        NotionSource.from_config({"kind": "notion", "hub_page_id": "hub", "buckets": bad})


def test_ingest_returns_assembled_records(fake_notion, tmp_path):
    records = NotionSource.from_config(_section(fake_notion)).ingest(tmp_path)
    assert records
    assert all(isinstance(r, CustomerRecord) for r in records)
    names = {r.canonical_name for r in records}
    assert "Acme Robotics" in names
    assert "Methodology" not in names  # a reference page, not a customer


def test_ingest_merges_the_curated_inputs_in_the_data_dir(fake_notion, tmp_path):
    (tmp_path / "self-managed-customers.yaml").write_text(
        yaml.safe_dump(
            {"customers": [{"name": "Onprem Only Co", "deployment": "on-prem", "domains": []}]}
        )
    )
    records = NotionSource.from_config(_section(fake_notion)).ingest(tmp_path)
    assert "Onprem Only Co" in {r.canonical_name for r in records}


def test_ingest_without_a_token_raises_source_error(fake_notion, tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_API_TOKEN")
    with pytest.raises(SourceError, match="NOTION_API_TOKEN not set"):
        NotionSource.from_config(_section(fake_notion)).ingest(tmp_path)


def _config(tmp_path: Path, buckets: dict[str, str]) -> Path:
    path = tmp_path / "registry-sync.yaml"
    path.write_text(yaml.safe_dump({"source": _section(buckets), "data_dir": "data"}))
    return path


def test_cli_ingest_writes_the_notion_dataset_through_the_source(fake_notion, tmp_path):
    cfg = _config(tmp_path, fake_notion)
    assert cli.main(["--config", str(cfg), "ingest", "--only", "notion", "--no-input"]) == 0
    written = sorted((tmp_path / "data" / "datasets" / "notion").glob("*.yaml"))
    assert any(p.stem == "acmerobotics" for p in written)
    assert (tmp_path / "data" / "identities.yaml").is_file()


def test_cli_ingest_without_a_token_exits_2(fake_notion, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_API_TOKEN")
    cfg = _config(tmp_path, fake_notion)
    assert cli.main(["--config", str(cfg), "ingest", "--only", "notion", "--no-input"]) == 2
    assert "ERROR: NOTION_API_TOKEN not set (set it in the environment)." in (
        capsys.readouterr().err
    )
