"""`adopt-live`: migrate a v0.2.0 data repo to full ownership, offline."""

from pathlib import Path

import pytest
from data_repo import build_data_repo
from data_repo import run as run_repo
from gitops_engine.datasets import dataset_dir
from gitops_engine.fieldplan import DESTRUCTIVE
from gitops_engine.ignore import BUILTIN_PREFIX
from gitops_engine.manifest import write_manifest
from gitops_engine.mirror import reconcile_mirror

from customer_registry_sync.datasets import NOTION
from customer_registry_sync.deploy_plan import offline_plan
from customer_registry_sync.identity import IDENTITIES_FILE, IdentityStore, load_identities
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_files import IGNORE_FILE, RETIRE_FILE
from customer_registry_sync.linear_overrides import OVERRIDES_FILE, load_overrides_file
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.migrate import AdoptResult, adopt_live, identity_for_live
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import STATE_SUBDIR, write_dataset, write_state

DATASETS_YAML = "datasets: [notion, linear-found]\n"
ZERO = AdoptResult(pins=0, adds=0, hidden=0, prefixes=0, identities_added=0)


def _node(lid, name, *, domains=(), revenue=None, status="Active", tier=None, xids=()):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": revenue,
            "externalIds": list(xids),
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s", "name": status} if status else None,
            "tier": {"id": "t", "name": tier} if tier else None,
        }
    )


def _mirror(data, *nodes):
    write_state(reconcile_mirror(list(nodes), LINEAR_SCHEMA), data)
    write_manifest(data / STATE_SUBDIR, "sync-inbound")


def _repo(tmp_path, run_cli, *, records=(), nodes=(), later=()):
    """A v0.2.0-shaped data dir from the real producers and a real `ingest`. `later`
    customers join the mirror after the ingest, so they have no stub or identity, as
    ignored customers never got one."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "datasets.yaml").write_text(DATASETS_YAML)
    write_dataset(list(records), data, dataset=NOTION)
    write_manifest(dataset_dir(data, NOTION), "ingest")
    _mirror(data, *nodes)
    argv = ["ingest", "--only", "linear-found", "--out-dir", str(data), "--no-input"]
    assert run_cli(argv) == 0
    if later:
        _mirror(data, *nodes, *later)
    return data


def _adopt(run_cli, data, *extra):
    return run_cli(["adopt-live", "--out-dir", str(data), "--no-input", *extra])


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


ACME = CustomerRecord(canonical_name="Acme", domains=["acme.test"], classification="real")
ACME_LIVE = dict(domains=["acme.test", "acme.io"], revenue=5000, tier="Gold")


def test_adopt_records_linear_only_revenue_tier_domains(tmp_path, run_cli, capsys):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    capsys.readouterr()
    assert _adopt(run_cli, data) == 0
    out = capsys.readouterr().out
    assert "2 pins, 1 domain adds" in out
    assert "next: run generate, then check" in out
    entry = load_overrides_file(data).entries["acme"]
    assert entry.pin == {"revenue": 5000, "tier": "Gold"}
    assert entry.add == {"domains": ["acme.io"]}


def test_adopt_returns_counts(tmp_path, run_cli):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    assert adopt_live(data) == AdoptResult(pins=2, adds=1, hidden=0, prefixes=0, identities_added=0)


@pytest.mark.parametrize(
    ("curated", "live_domains"),
    [
        # the registry carries the domain in another case
        (None, ["ACME.test"]),
        # the overrides already add it in another case
        ("{add: [acme.io]}", ["acme.test", "Acme.IO"]),
        # a curated remove stands, whatever the case
        ("{remove: [acme.io]}", ["acme.test", "ACME.io"]),
    ],
)
def test_adopt_writes_no_case_duplicate_domains(tmp_path, run_cli, curated, live_domains):
    node = _node("lin_a", "Acme", domains=live_domains)
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[node])
    if curated is not None:
        (data / OVERRIDES_FILE).write_text(f"customers:\n  acme:\n    domains: {curated}\n")
    before = load_overrides_file(data).entries.get("acme")
    assert adopt_live(data).adds == 0
    assert load_overrides_file(data).entries.get("acme") == before


def test_adopt_keeps_an_existing_pin(tmp_path, run_cli):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    (data / OVERRIDES_FILE).write_text("customers:\n  acme:\n    pin: {tier: null}\n")
    result = adopt_live(data)
    assert (result.pins, result.adds) == (1, 1)
    assert load_overrides_file(data).entries["acme"].pin == {"tier": None, "revenue": 5000}


def test_adopt_skips_stubs_and_unpaired(tmp_path, run_cli):
    lonely = CustomerRecord(canonical_name="Lonely", domains=["lonely.test"], classification="real")
    stub = _node("lin_s", "Stubby", domains=["stubby.test"], revenue=10, tier="Gold")
    data = _repo(tmp_path, run_cli, records=[ACME, lonely], nodes=[_node("lin_a", "Acme"), stub])
    assert "stubby" in load_identities(data / IDENTITIES_FILE).entries
    assert adopt_live(data) == ZERO
    assert not (data / OVERRIDES_FILE).exists()


SANDBOX = _node("lin_x", "Acme Sandbox [Active]", domains=["sandbox.test"], revenue=1)
SANDBOX_TWO = _node("lin_y", "Acme Sandbox", revenue=2)


def _ignore(data, *entries, prefixes=()):
    lines = ["customers:"] if entries else []
    for lid, name, reason in entries:
        lines.append(f"  - {{linear_id: {lid}, name: {name}, reason: {reason}}}")
    if prefixes:
        lines.append("name_prefixes:")
        lines += [f'  - {{prefix: "{p}", reason: {r}}}' for p, r in prefixes]
    (data / IGNORE_FILE).write_text("\n".join(lines) + "\n")


def _hidden_ids(data):
    return {cid: e.hidden for cid, e in load_overrides_file(data).entries.items() if e.hidden}


def test_adopt_converts_ignore_with_existing_identity(tmp_path, run_cli):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme")], later=[SANDBOX])
    store = IdentityStore(data / IDENTITIES_FILE)
    store.add_identity("sandbox-co", sources={"linear": "lin_x"})
    store.save()
    _ignore(data, ("lin_x", "Acme Sandbox", "test account"))
    result = adopt_live(data)
    assert (result.hidden, result.identities_added) == (1, 0)
    assert _hidden_ids(data) == {"sandbox-co": "test account"}
    assert not (data / IGNORE_FILE).exists()
    assert set(load_identities(data / IDENTITIES_FILE).entries) == {"acme", "sandbox-co"}


def test_adopt_converts_ignore_without_identity(tmp_path, run_cli):
    data = _repo(
        tmp_path,
        run_cli,
        records=[ACME],
        nodes=[_node("lin_a", "Acme")],
        later=[SANDBOX, SANDBOX_TWO],
    )
    _ignore(data, ("lin_x", "Acme Sandbox", "test account"), ("lin_y", "Acme Sandbox", "demo"))
    result = adopt_live(data)
    assert (result.hidden, result.identities_added) == (2, 2)
    table = load_identities(data / IDENTITIES_FILE).entries
    assert table["acme-sandbox"].sources == {"linear": "lin_x"}
    assert table["acme-sandbox-2"].sources == {"linear": "lin_y"}
    assert _hidden_ids(data) == {"acme-sandbox": "test account", "acme-sandbox-2": "demo"}
    # the hidden customers are outside the managed set: the plan writes nothing to them
    assert run_cli(["generate", "--out-dir", str(data), "--no-input"]) == 0
    plan, code = offline_plan(data)
    assert code == 0
    assert plan.orphans == [] and set(plan.changes) <= {"lin_a"}


def test_identity_for_live_finds_or_makes_one(tmp_path):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    store.add_identity("kept", sources={"linear": "lin_k"})
    assert identity_for_live(store, "lin_k", "Anything") == ("kept", False)
    assert identity_for_live(store, "lin_n", "New Co [Prospect]") == ("new-co", True)
    assert identity_for_live(store, "lin_n", "New Co") == ("new-co", False)
    # a name with nothing to slug falls back to the Linear id
    assert identity_for_live(store, "lin_z", "!!!") == ("lin_z", True)


def test_adopt_converts_prefixes_not_builtin(tmp_path, run_cli):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme")])
    _ignore(data, prefixes=[("sandbox-", "throwaways"), (BUILTIN_PREFIX, "harness")])
    assert adopt_live(data).prefixes == 1
    prefixes = load_overrides_file(data).prefixes
    assert [(p.prefix, p.reason) for p in prefixes if p.line is not None] == [
        ("sandbox-", "throwaways")
    ]
    assert [p.prefix for p in prefixes if p.line is None] == [BUILTIN_PREFIX]


def test_adopt_refuses_nonempty_retire(tmp_path, run_cli, capsys):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    _ignore(data, ("lin_x", "Acme Sandbox", "test account"))
    (data / RETIRE_FILE).write_text("acme:\n  tier: Gold\n")
    before = _files(data)
    assert _adopt(run_cli, data) == 2
    assert "linear-retire.yaml" in capsys.readouterr().err
    assert _files(data) == before


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (IDENTITIES_FILE, "acme: [not, a, mapping]\n"),
        (IGNORE_FILE, "customers:\n  - {linear_id: lin_x}\n"),  # no reason
        (OVERRIDES_FILE, "customers:\n  acme:\n    pin: {domains: [a.test]}\n"),
        (RETIRE_FILE, "- not a mapping\n"),
    ],
)
def test_adopt_refuses_unreadable_files(tmp_path, run_cli, name, text):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    (data / name).write_text(text)
    before = _files(data)
    assert _adopt(run_cli, data) == 2
    assert _files(data) == before


@pytest.mark.parametrize(
    "text", ["- not a mapping\n", "canonical_name: Acme\nclassification: bogus\n"]
)
def test_adopt_refuses_unreadable_dataset(tmp_path, run_cli, capsys, text):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    from gitops_engine.manifest import MANIFEST

    [record] = [p for p in dataset_dir(data, NOTION).glob("*.yaml") if p.name != MANIFEST]
    record.write_text(text)
    before = _files(data)
    assert _adopt(run_cli, data) == 2
    err = capsys.readouterr().err
    assert "datasets under" in err and "cannot be read" in err and "Traceback" not in err
    assert "adopt-live refused; nothing written." in err
    assert _files(data) == before


def test_adopt_refuses_without_mirror(tmp_path, run_cli):
    data = tmp_path / "data"
    data.mkdir()
    write_dataset([ACME], data, dataset=NOTION)
    before = _files(data)
    assert _adopt(run_cli, data) == 2
    assert _files(data) == before


@pytest.mark.parametrize("text", ["", "# acknowledged removals\n", "{}\n"])
def test_adopt_deletes_empty_retire(tmp_path, run_cli, text):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme")])
    (data / RETIRE_FILE).write_text(text)
    assert _adopt(run_cli, data) == 0
    assert not (data / RETIRE_FILE).exists()


def test_adopt_dry_run_writes_nothing(tmp_path, run_cli, capsys):
    data = _repo(tmp_path, run_cli, records=[ACME], nodes=[_node("lin_a", "Acme", **ACME_LIVE)])
    _ignore(data, ("lin_x", "Acme Sandbox", "test account"))
    (data / RETIRE_FILE).write_text("")
    before = _files(data)
    capsys.readouterr()
    assert _adopt(run_cli, data, "--dry-run") == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert _files(data) == before
    assert adopt_live(data, dry_run=True) == AdoptResult(
        pins=2, adds=1, hidden=1, prefixes=0, identities_added=1
    )
    assert _files(data) == before


# --- on the synthetic data repo ---------------------------------------------------


def _non_destructive(plan):
    return {lid: [c for c in cs if c.kind not in DESTRUCTIVE] for lid, cs in plan.changes.items()}


def test_adopt_then_generate_plan_has_no_destructive_change(tmp_path):
    root = build_data_repo(tmp_path / "repo")
    data = root / "customers"
    before, _ = offline_plan(data)
    destructive = [c for cs in before.changes.values() for c in cs if c.kind in DESTRUCTIVE]
    assert destructive, "the fixture repo must hold a Linear-only value to keep"
    assert run_repo(root, "adopt-live", "--no-input") == 0
    assert run_repo(root, "generate", "--no-input") == 0
    after, code = offline_plan(data)
    assert code == 0
    assert [c for cs in after.changes.values() for c in cs if c.kind in DESTRUCTIVE] == []
    assert after.creates == before.creates
    assert [lv.id for _, lv in after.updates] == [lv.id for _, lv in before.updates]
    assert {k: v for k, v in after.changes.items() if v} == {
        k: v for k, v in _non_destructive(before).items() if v
    }


def test_adopt_is_idempotent(tmp_path):
    root = build_data_repo(tmp_path / "repo")
    data = root / "customers"
    _ignore(data, ("lin_x", "Acme Sandbox", "test account"), prefixes=[("sandbox-", "throwaways")])
    first = adopt_live(data)
    assert first.pins > 0 and first.hidden == 1 and first.prefixes == 1
    files = _files(data)
    assert adopt_live(data) == ZERO
    assert _files(data) == files
    assert (data / OVERRIDES_FILE).read_bytes() == files[OVERRIDES_FILE]


def test_adopt_runs_without_token(tmp_path, monkeypatch):
    from customer_registry_sync import linear_client

    class NoLinear:
        def __init__(self, *a, **k):
            raise AssertionError("adopt-live must not reach Linear")

    root = build_data_repo(tmp_path / "repo")
    monkeypatch.setattr(linear_client, "LinearClient", NoLinear)
    assert run_repo(root, "adopt-live", "--no-input") == 0
