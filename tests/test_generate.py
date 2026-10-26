import inspect

import pytest

from customer_registry_sync import cli
from customer_registry_sync.datasets import NOTION, dataset_dir
from customer_registry_sync.generate import generate_linear, strip_status_suffix
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
