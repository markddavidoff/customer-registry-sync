import inspect

import pytest
from gitops_engine.datasets import dataset_dir

from customer_registry_sync import cli
from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetConfig, load_datasets
from customer_registry_sync.generate import (
    generate_from_dir,
    generate_linear,
    merge_datasets,
    project_record,
)
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities, strip_status_suffix
from customer_registry_sync.identity_store import IdentityStore
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import write_dataset


def ds(**kw) -> CustomerRecord:
    base = dict(
        canonical_name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        classification="real",
        arr=250000.0,
        tier="Tier 1",
    )
    base.update(kw)
    return CustomerRecord(**base)


def test_projects_real_record_to_active_linear_customer():
    [lc] = generate_linear([ds()])
    assert lc.name == "Acme Robotics [Active]"
    assert lc.domains == ["acmerobotics.example.test"]
    assert lc.revenue == 250000  # arr float -> int
    assert lc.tier == "Tier 1"
    assert lc.status == "Active"
    assert lc.external_ids == ["acmerobotics"]  # slug, the stable link
    assert lc.linear_synced is None


def test_arr_rounded_to_int():
    [lc] = generate_linear([ds(arr=5000000.04)])
    assert lc.revenue == 5000000
    [lc2] = generate_linear([ds(arr=None)])
    assert lc2.revenue is None


def test_classification_to_status_map_and_internal_excluded():
    recs = [
        ds(canonical_name="R", domains=["r.com"], classification="real"),
        ds(canonical_name="P", domains=["p.com"], classification="prospect"),
        ds(canonical_name="C", domains=["c.com"], classification="churned"),
        ds(canonical_name="Pa", domains=["pa.com"], classification="partner"),
        ds(canonical_name="Int", domains=["int.com"], classification="internal"),
    ]
    out = {lc.name: lc.status for lc in generate_linear(recs)}
    assert out == {
        "R [Active]": "Active",
        "P [Prospect]": "Prospect",
        "C [Churned]": "Churned",
        "Pa [Active]": "Active",
    }
    assert not any(n.startswith("Int") for n in out)  # internal is not projected to Linear


def test_generate_is_idempotent():
    recs = [ds(canonical_name="A", domains=["a.com"]), ds(canonical_name="B", domains=["b.com"])]
    assert generate_linear(recs) == generate_linear(recs)


def test_generate_takes_dataset_only_and_never_emits_state():
    assert list(inspect.signature(generate_linear).parameters) == ["dataset_recs"]
    recs = [
        CustomerRecord(
            canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1200.4
        )
    ]
    [out] = generate_linear(recs)
    assert out.linear_synced is None
    assert out.external_ids == ["acme"] and out.revenue == 1200 and out.status == "Active"


def test_name_carries_status_suffix_for_each_status():
    recs = [
        ds(canonical_name="Gringotts Advisory", domains=["g.example.test"], classification="real"),
        ds(canonical_name="Pendant Devices", domains=["p.example.test"], classification="prospect"),
        ds(canonical_name="Soylent Metrics", domains=["r.com"], classification="churned"),
    ]
    assert sorted(lc.name for lc in generate_linear(recs)) == [
        "Gringotts Advisory [Active]",
        "Pendant Devices [Prospect]",
        "Soylent Metrics [Churned]",
    ]


def test_strip_status_suffix_only_strips_a_trailing_status():
    assert strip_status_suffix("Gringotts Advisory [Active]") == "Gringotts Advisory"
    assert strip_status_suffix("Pendant Devices [Prospect]") == "Pendant Devices"
    assert strip_status_suffix("Soylent Metrics [Churned]") == "Soylent Metrics"
    assert strip_status_suffix("Gringotts Advisory") == "Gringotts Advisory"
    assert strip_status_suffix("Acme [Beta]") == "Acme [Beta]"
    assert strip_status_suffix("Acme [Active] Holdings") == "Acme [Active] Holdings"
    assert strip_status_suffix("[Active]") == "[Active]"


@pytest.mark.parametrize("create_empty_dataset_dir", [False, True])
def test_generate_cli_warns_when_dataset_is_missing_or_empty(
    tmp_path, make_config, capsys, create_empty_dataset_dir
):
    if create_empty_dataset_dir:
        dataset_dir(tmp_path, NOTION).mkdir(parents=True)
    cfg = make_config()
    assert cli.main(["--config", str(cfg), "generate", "--out-dir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "WARNING: no dataset records" in err and "run `ingest` first" in err


def test_generate_cli_is_silent_on_stderr_with_a_dataset(tmp_path, make_config, capsys):
    write_dataset([ds()], tmp_path)
    cfg = make_config()
    assert cli.main(["--config", str(cfg), "generate", "--out-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""
    assert (tmp_path / "linear" / "acmerobotics.yaml").exists()


# ---- merge_datasets: one row per identity, the Notion record wins whole ----


def _entries(tmp_path, notion=(), found=(), found_stems=None):
    """DatasetRecords as `generate` reads them: written by `write_dataset`, loaded back."""
    if notion:
        write_dataset(list(notion), tmp_path)
    if found:
        write_dataset(list(found), tmp_path, dataset=LINEAR_FOUND, stems=found_stems)
    return load_datasets(tmp_path, DatasetConfig(names=[NOTION, LINEAR_FOUND]))


def _table(tmp_path, *identities):
    """An identity table written by the real producer; each identity is
    (canonical id, names, domains, sources)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        store.add_identity(cid, names=names, domains=domains, sources=sources)
    store.save()
    return load_identities(tmp_path / IDENTITIES_FILE)


def _stub(**kw) -> CustomerRecord:
    base = {
        "canonical_name": "Acme Robotics",
        "domains": ["acmerobotics.example.test"],
        "classification": "real",
        "arr": 1.0,
        "linear_customer_id": "lin_1",
        "sources": ["linear:lin_1"],
    }
    base.update(kw)
    return CustomerRecord(**base)


PRIORITY = [NOTION, LINEAR_FOUND]


def test_record_in_both_datasets_uses_notion_whole(tmp_path):
    entries = _entries(
        tmp_path, notion=[ds(arr=None)], found=[_stub()], found_stems=["acmerobotics"]
    )
    table = _table(
        tmp_path,
        (
            "acmerobotics",
            ["Acme Robotics"],
            ["acmerobotics.example.test"],
            {"notion": "acmerobotics", "linear": "lin_1"},
        ),
    )
    got = merge_datasets(entries, table, PRIORITY)
    [row] = got.rows
    assert row.revenue is None  # the Notion value, not the stub's 1
    assert row.linear_owned is False
    assert row.name == "Acme Robotics [Active]"
    [winner] = got.winners
    assert winner.dataset == NOTION


def test_identity_domains_appended_after_record_domains(tmp_path):
    entries = _entries(
        tmp_path, notion=[ds(canonical_name="Hooli", domains=["hooli.example.test"])]
    )
    table = _table(
        tmp_path,
        (
            "hooli",
            ["Hooli"],
            ["extra.hooli.example.test", "HOOLI.example.test"],
            {"notion": "hooli"},
        ),
    )
    got = merge_datasets(entries, table, PRIORITY)
    [row] = got.rows
    assert row.domains == ["hooli.example.test", "extra.hooli.example.test"]
    assert got.winners[0].record.domains == ["hooli.example.test", "extra.hooli.example.test"]


def test_external_id_is_canonical_id_not_slug(tmp_path):
    rec = ds(canonical_name="Initrode Systems", domains=["initrode-systems.example.test"])
    entries = _entries(tmp_path, notion=[rec])
    assert entries[0].source_id == "initrode-systems"
    table = _table(tmp_path, ("initrode", ["Initrode"], [], {"notion": "initrode-systems"}))
    [row] = merge_datasets(entries, table, PRIORITY).rows
    assert row.external_ids == ["initrode"]


def test_stub_row_is_linear_owned_without_suffix(tmp_path):
    entries = _entries(
        tmp_path,
        found=[_stub(canonical_name="Umbrella Analytics", domains=["umbrella.example.test"])],
        found_stems=["umbrella"],
    )
    table = _table(
        tmp_path,
        ("umbrella", ["Umbrella Analytics"], ["umbrella.example.test"], {"linear": "lin_1"}),
    )
    [row] = merge_datasets(entries, table, PRIORITY).rows
    assert row.linear_owned is True
    assert row.name == "Umbrella Analytics"
    assert row.external_ids == ["umbrella"]
    assert row.revenue == 1


def test_unresolved_record_keeps_slug_as_external_id(tmp_path):
    entries = _entries(tmp_path, notion=[ds()])
    [row] = merge_datasets(entries, _table(tmp_path), PRIORITY).rows
    assert row.external_ids == ["acmerobotics"] and row == generate_linear([ds()])[0]


def test_project_record_suffix_and_internal():
    assert project_record(ds(), external_id="x", linear_owned=True).name == "Acme Robotics"
    assert project_record(ds(), external_id="x").name == "Acme Robotics [Active]"
    assert project_record(ds(classification="internal"), external_id="x") is None


def test_internal_record_is_a_winner_but_not_a_row(tmp_path):
    entries = _entries(tmp_path, notion=[ds(classification="internal")])
    got = merge_datasets(entries, _table(tmp_path), PRIORITY)
    assert got.rows == [] and [w.record.canonical_name for w in got.winners] == ["Acme Robotics"]


def test_generate_from_dir_reads_datasets_and_identities(tmp_path):
    _entries(tmp_path, notion=[ds(canonical_name="Hooli", domains=["hooli.example.test"])])
    _table(tmp_path, ("hooli", ["Hooli"], ["extra.hooli.example.test"], {"notion": "hooli"}))
    [row] = generate_from_dir(tmp_path).rows
    assert row.external_ids == ["hooli"]
    assert row.domains == ["hooli.example.test", "extra.hooli.example.test"]
