from dataclasses import replace

import pytest

from gitops_engine.datasets import dataset_dir
from gitops_engine.hidden import hidden_from
from gitops_engine.manifest import MANIFEST, write_manifest
from gitops_engine.mirror import reconcile_mirror

from gitops_engine import deploy

from customer_registry_sync import cli
from customer_registry_sync.check import (
    EXIT_DRIFT,
    EXIT_OK,
    detect_drift,
    identity_offline_errors,
    offline_errors,
    orphan_lines,
    orphans,
    run_check,
    unused_identities,
)
from customer_registry_sync.datasets import (
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    DatasetRecord,
    load_datasets,
)
from gitops_engine.fieldplan import FieldChange
from customer_registry_sync.generate import generate_from_dir, merge_datasets
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_client import LiveCustomer, parse_customer_node
from customer_registry_sync.linear_overrides import OVERRIDES_FILE, load_overrides_file
from customer_registry_sync.linear_schema import (
    LINEAR_SCHEMA,
    LinearCustomer,
    LinearSynced,
    content_hash_of,
)
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    write_csvs,
    write_dataset,
    write_desired,
    write_state,
)


def _live(lid, name, updated="2026-10-01T00:00:00.000Z", revenue=1):
    return LiveCustomer(
        id=lid,
        name=name,
        domains=[f"{name.lower()}.example.test"],
        revenue=revenue,
        tier=None,
        status="Active",
        external_ids=[name.lower()],
        updated_at=updated,
    )


def _state_from(lv):
    s = LINEAR_SCHEMA.mirror_row(lv)
    s.linear_synced = LinearSynced(lv.id, lv.updated_at, content_hash_of(s))
    return s


def test_no_drift_when_state_matches_live():
    lv = _live("lin_1", "Acme")
    assert not detect_drift([_state_from(lv)], [lv]).any()


def test_changed_when_content_differs_even_if_updated_at_same():
    lv = _live("lin_1", "Acme")
    st = _state_from(lv)
    now = _live("lin_1", "Acme", revenue=42)
    d = detect_drift([st], [now])
    assert [s.name for s, _ in d.changed] == ["Acme"] and not d.added and not d.deleted


def test_metadata_only_bump_is_not_changed():
    lv = _live("lin_1", "Acme")
    now = _live(
        "lin_1", "Acme", updated="2026-10-09T00:00:00.000Z"
    )  # same content, newer updatedAt
    assert not detect_drift([_state_from(lv)], [now]).any()


def test_added_and_deleted_classes():
    a, b = _live("lin_a", "Alpha"), _live("lin_b", "Beta")
    d = detect_drift([_state_from(a)], [b])  # a vanished, b appeared
    assert [x.name for x in d.deleted] == ["Alpha"] and [x.name for x in d.added] == ["Beta"]


def test_detect_drift_skips_untracked_state_rows():
    untracked = LinearCustomer(name="Loose", status="Active")  # linear_synced None
    lv = _live("lin_1", "Acme")
    d = detect_drift([untracked, _state_from(lv)], [lv])
    assert not d.changed and not d.deleted and not d.added


def test_run_check_exit_codes():
    lv = _live("lin_1", "Acme")
    desired = LinearCustomer(
        name="Acme",
        domains=["acme.example.test"],
        revenue=1,
        status="Active",
        external_ids=["acme"],
    )
    res = run_check([desired], [_state_from(lv)], [lv])
    assert res.code == EXIT_OK and not res.drift.any() and res.plan.unchanged
    assert res.untracked == [] and res.stale == []
    res = run_check([desired], [_state_from(lv)], [_live("lin_1", "Acme", revenue=9)])
    assert res.code == EXIT_DRIFT and res.drift.changed


def _desired_acme(**kw):
    base = {
        "name": "Acme",
        "domains": ["acme.example.test"],
        "revenue": 1,
        "status": "Active",
        "external_ids": ["acme"],
    }
    return LinearCustomer(**{**base, **kw})


def test_check_exits_3_on_unacked_destructive(monkeypatch):
    lv = _live("lin_1", "Acme")
    bad = FieldChange("revenue", 1, None, "clear")  # retire=False: a planner bug
    monkeypatch.setattr(deploy, "plan_row", lambda d, live, retire, **_: (d, [bad]))
    res = run_check([_desired_acme()], [_state_from(lv)], [lv])
    assert res.plan.unacked and res.code == EXIT_DRIFT
    assert not res.drift.any() and not res.untracked and not res.stale


class _Fake:
    def __init__(self, live):
        self.live = live

    def list_customers(self):
        return list(self.live)


def test_cli_check_exit_3_on_drift_and_prints_classes(tmp_path, monkeypatch, capsys, run_cli):
    lv = _live("lin_1", "Acme")
    write_desired(
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=1,
                status="Active",
                external_ids=["acme"],
            )
        ],
        tmp_path,
    )
    write_state([_state_from(lv)], tmp_path)
    monkeypatch.setattr(
        cli,
        "_linear_client",
        lambda: _Fake([_live("lin_1", "Acme", revenue=9), _live("lin_2", "Newco")]),
    )
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "DRIFT changed: Acme" in out and "DRIFT added: Newco" in out
    assert (
        out.rstrip().splitlines()[-1]
        == "check FAILED: see the ERROR, DRIFT, PENDING, OVERRIDE and OFFLINE lines above"
    )
    assert "run sync-inbound, resolve, re-run" not in out


def _generated_tree(tmp_path):
    """A data dir built by the real producers: dataset -> generate -> sync-inbound mirror.
    Returns the dataset records and the live customer the mirror was taken from."""
    recs = [
        CustomerRecord(
            canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
        )
    ]
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    gen = generate_from_dir(tmp_path)
    [desired] = gen.rows
    write_desired([desired], tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    lv = LiveCustomer(
        id="lin_1",
        name=desired.name,
        domains=desired.domains,
        revenue=desired.revenue,
        tier=desired.tier,
        status=desired.status,
        external_ids=desired.external_ids,
        updated_at="2026-10-01T00:00:00.000Z",
    )
    write_state([_state_from(lv)], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    return recs, lv


def test_cli_check_in_sync_exits_0(tmp_path, monkeypatch, capsys, run_cli):
    _, lv = _generated_tree(tmp_path)
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.rstrip().endswith("IN SYNC")


def test_cli_check_fails_on_offline_errors(tmp_path, monkeypatch, capsys, run_cli):
    _, lv = _generated_tree(tmp_path)
    p = tmp_path / "linear" / "acme.yaml"
    p.write_text(p.read_text() + "# tampered\n")
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    assert (
        "OFFLINE: hand-edited generated file: linear/acme.yaml: run generate"
        in capsys.readouterr().out
    )


# ---- offline_errors: generated-file manifests + regenerate check ----


def test_offline_errors_clean(tmp_path):
    _generated_tree(tmp_path)
    assert offline_errors(tmp_path) == []


def test_offline_errors_on_hand_edit(tmp_path):
    _generated_tree(tmp_path)
    p = tmp_path / "linear-state" / "acme.yaml"
    p.write_text(p.read_text() + "# edited\n")
    assert offline_errors(tmp_path) == [
        "hand-edited generated file: linear-state/acme.yaml: run sync-inbound"
    ]


def test_offline_errors_on_missing_manifest(tmp_path):
    _generated_tree(tmp_path)
    (dataset_dir(tmp_path, NOTION) / MANIFEST).unlink()
    assert offline_errors(tmp_path) == ["missing manifest: datasets/notion"]


def test_offline_errors_on_stale_generate(tmp_path):
    recs, _ = _generated_tree(tmp_path)
    recs[0].arr = 99.0  # dataset moved on; linear/ was not regenerated
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    assert offline_errors(tmp_path) == [
        "linear out of date: acme: run generate",
        "registry.csv out of date: run generate",
        "real-customers.csv out of date: run generate",
    ]


def test_cli_generate_writes_rows_and_csvs_that_check_accepts(tmp_path, capsys, run_cli):
    write_dataset(
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real"
            )
        ],
        tmp_path,
    )
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_state([], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    assert run_cli(["generate", "--out-dir", str(tmp_path), "--no-input"]) == 0
    assert (tmp_path / "linear" / "acme.yaml").exists()
    assert "Acme" in (tmp_path / "registry.csv").read_text()
    assert offline_errors(tmp_path) == []


def test_offline_errors_no_longer_read_the_ignore_file(tmp_path):
    # `check` and `deploy` refuse a legacy ignore file before planning
    # (`deploy_plan.legacy_refused`); the offline errors no longer validate it
    _generated_tree(tmp_path)
    (tmp_path / "linear-ignore.yaml").write_text("customers:\n  - linear_id: lin_a\n")
    assert offline_errors(tmp_path) == []


def test_offline_errors_on_stale_csv(tmp_path):
    _generated_tree(tmp_path)
    p = tmp_path / "registry.csv"
    p.write_text(p.read_text().replace("Acme", "Acme Corp"))
    assert offline_errors(tmp_path) == ["registry.csv out of date: run generate"]


def _store(tmp_path, *identities):
    """An identity table written by the real producer; each identity is
    (canonical id, names, domains, sources)."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        store.add_identity(cid, names=names, domains=domains, sources=sources)
    store.save()
    return load_identities(tmp_path / IDENTITIES_FILE)


def test_two_records_in_one_dataset_on_one_identity_fail(tmp_path):
    recs = [
        CustomerRecord(
            canonical_name="Initech Technologies",
            domains=["initech.example.test"],
            classification="real",
        ),
        CustomerRecord(
            canonical_name="Initech", domains=["initechtech.example.test"], classification="real"
        ),
    ]
    write_dataset(recs, tmp_path)
    entries = load_datasets(tmp_path, DatasetConfig())
    assert sorted(e.source_id for e in entries) == ["initech", "initechtech"]
    table = _store(
        tmp_path,
        (
            "initech",
            ["Initech Technologies", "Initech"],
            ["initech.example.test"],
            {"notion": "initech"},
        ),
    )
    assert identity_offline_errors(entries, table) == [
        "two records in datasets/notion resolve to initech: initech.yaml, initechtech.yaml",
    ]
    # offline_errors carries it too (generate keeps the first by file name)
    gen = generate_from_dir(tmp_path)
    assert [w.source_id for w in gen.winners] == ["initech"]
    assert (
        "two records in datasets/notion resolve to initech: initech.yaml, initechtech.yaml"
        in offline_errors(tmp_path)
    )


def test_record_resolving_to_two_identities_fails(tmp_path):
    write_dataset(
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real"
            )
        ],
        tmp_path,
    )
    entries = load_datasets(tmp_path, DatasetConfig())
    table = _store(
        tmp_path, ("acme", [], [], {"notion": "acme"}), ("acme-corp", [], ["acme.example.test"], {})
    )
    [err] = identity_offline_errors(entries, table)
    assert err == (
        "Acme resolves to two identities: acme (source id 'acme') "
        "and acme-corp (domain 'acme.example.test')"
    )


def test_check_exits_3_on_plan_errors(tmp_path):
    # two live customers on one identity, neither stamped nor recorded
    table = _store(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {}))
    a, b = (replace(_live(lid, "Acme"), external_ids=[]) for lid in ("lin_a", "lin_b"))
    res = run_check([_desired_acme()], [_state_from(a), _state_from(b)], [a, b], table)
    assert res.code == EXIT_DRIFT and res.plan.errors
    assert not res.drift.any() and not res.untracked and not res.stale


def test_identity_offline_errors_include_table_errors(tmp_path):
    table = _store(
        tmp_path, ("a", ["A"], ["x.example.test"], {}), ("b", ["B"], ["y.example.test"], {})
    )
    table.entries["b"].values["domains"].append(
        "x.example.test"
    )  # the store refuses this; a human edit does not
    assert identity_offline_errors([], table) == [
        "domain 'x.example.test' is on two identities: a, b"
    ]


# ---- the one managed/hidden partition ----

_HIDE_OWN = "customers:\n  own:\n    hidden: {reason: our own company}\n"


def _hidden_own(tmp_path):
    """(hidden, table): `own` is hidden in linear-overrides.yaml, and its identity
    records Pied Piper's Linear id, through which the live row reaches it."""
    table = _store(tmp_path, ("own", ["Pied Piper"], [], {"linear": "lin_own"}))
    (tmp_path / OVERRIDES_FILE).write_text(_HIDE_OWN)
    return hidden_from(load_overrides_file(tmp_path)), table


def _node(
    lid,
    name,
    *,
    domains=(),
    revenue=1,
    xids=(),
    status="Active",
    updated="2026-10-01T00:00:00.000Z",
    tier=None,
):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": revenue,
            "externalIds": list(xids),
            "updatedAt": updated,
            "status": {"id": "s", "name": status} if status else None,
            "tier": {"id": "t", "name": tier} if tier else None,
        }
    )


def _managed_acme():
    return _node("lin_1", "Acme", domains=["acme.example.test"], xids=["acme"])


_TWO_STAMPED = (
    "two or more Linear customers carry the canonical id acme: 'Acme' (lin_1), "
    "'Pied Piper' (lin_own): merge them in Linear or remove the id from all but one"
)


def test_hidden_not_in_drift(tmp_path):
    hid, table = _hidden_own(tmp_path)
    acme = _managed_acme()
    own_then = _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    own_now = _node(
        "lin_own",
        "Pied Piper Inc",
        domains=["piedpiper.example.test"],
        revenue=7,
        updated="2026-10-05T00:00:00.000Z",
    )
    state = [_state_from(acme), _state_from(own_then)]
    res = run_check([_desired_acme()], state, [acme, own_now], table, hidden=hid)
    assert not res.drift.any() and res.code == EXIT_OK
    assert [lv.id for lv in res.ignored] == ["lin_own"]
    # a hidden customer deleted from Linear is not "deleted" drift either
    res = run_check([_desired_acme()], state, [acme], table, hidden=hid)
    assert not res.drift.any()
    # control: without the hidden entry the same edit is drift
    assert run_check([_desired_acme()], state, [acme, own_now], table).drift.changed


def test_hidden_not_in_plan(tmp_path):
    hid, table = _hidden_own(tmp_path)
    acme, own = _managed_acme(), _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    state = [_state_from(acme), _state_from(own)]
    res = run_check([_desired_acme()], state, [acme, own], table, hidden=hid)
    assert res.plan.orphans == [] and not res.plan.updates and not res.plan.creates
    assert [d.name for d in res.plan.unchanged] == ["Acme"]
    assert run_check([_desired_acme()], state, [acme, own], table).plan.orphans == [own]


def test_hidden_not_untracked(tmp_path):
    # the hidden customer carries the canonical id too and comes first, so an
    # unpartitioned planner would pair it and report its extra domain
    hid, table = _hidden_own(tmp_path)
    acme = _managed_acme()
    own = _node(
        "lin_own",
        "Pied Piper",
        domains=["acme.example.test", "piedpiper.example.test"],
        xids=["acme"],
    )
    res = run_check([_desired_acme()], [], [own, acme], table, hidden=hid)
    assert res.untracked == []
    assert [d.name for d in res.plan.unchanged] == ["Acme"]
    # it shares Acme's canonical id, so it is a hidden conflict instead
    assert res.failures == [
        (
            "desired record 'Acme' shares source id 'acme' with hidden Linear customer "
            "'Pied Piper' (entry own, linear-overrides.yaml). "
            "Remove that entry from linear-overrides.yaml or change Acme."
        )
    ]
    assert res.code == EXIT_DRIFT
    # control: unpartitioned, both carry the canonical id, so the planner refuses
    assert run_check([_desired_acme()], [], [own, acme], table).plan.errors == [_TWO_STAMPED]


def test_linear_edit_to_stub_is_not_drift(tmp_path):
    table = _store(tmp_path, ("hooli", ["Hooli"], ["hooli.example.test"], {"linear": "lin_hooli"}))
    stub = CustomerRecord(
        canonical_name="Hooli",
        domains=["hooli.example.test"],
        classification="real",
        linear_customer_id="lin_hooli",
        sources=["linear:lin_hooli"],
    )
    [d] = merge_datasets(
        [DatasetRecord(LINEAR_FOUND, "hooli", stub)], table, [NOTION, LINEAR_FOUND]
    ).rows
    assert d.linear_owned
    then = _node("lin_hooli", "Hooli", domains=["hooli.example.test"], xids=["hooli"])
    now = _node(
        "lin_hooli",
        "Hooli Inc",
        domains=["hooli.example.test", "hooli.example.org"],
        revenue=40,
        status="Prospect",
        xids=["hooli"],
        updated="2026-10-09T00:00:00.000Z",
    )
    res = run_check([d], [_state_from(then)], [now], table)
    assert not res.drift.changed and not res.plan.aborts
    assert res.code == EXIT_OK
    # a registry-owned row with the same kind of edit is still drift
    acme = _managed_acme()
    acme_now = replace(acme, revenue=40, updated_at="2026-10-09T00:00:00.000Z")
    res = run_check([_desired_acme()], [_state_from(acme)], [acme_now])
    assert res.drift.changed and res.plan.aborts


def test_unmapped_status_fails_naming_customer_and_status(tmp_path, run_cli):
    """A stub whose Linear status is not in the table is written with
    `classification: null`, and `check` fails naming the customer and the status."""
    (tmp_path / "datasets.yaml").write_text(
        "datasets: [notion, linear-found]\n"
        "linear_status_classification: {Active: real, Prospect: prospect, Churned: churned}\n"
    )
    write_dataset([], tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    write_state([_state_from(_node("lin_p", "Pilot Co", status="POC"))], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    assert (
        run_cli(["ingest", "--only", "linear-found", "--out-dir", str(tmp_path), "--no-input"]) == 0
    )
    [stub] = load_datasets(tmp_path, DatasetConfig(names=[LINEAR_FOUND]))
    assert stub.record.classification is None
    assert stub.record.status == "POC"
    assert (
        "unmapped Linear status: 'Pilot Co' (lin_p) has status 'POC'. Add 'POC: prospect' "
        "(or the right class) to linear_status_classification in datasets.yaml, "
        'or hide it: customer-registry-sync hide --linear-id lin_p --reason "<why>"'
    ) in offline_errors(tmp_path)


# ---- orphans, failure lines and reports ----

_ORPHAN_HOOLI = [
    "orphan: Linear customer 'Hooli' (lin_hooli) is in no dataset.",
    (
        "  Add it: dispatch the registry-sync workflow with mode=refresh (or run sync-inbound, "
        "ingest --only linear-found, generate) and merge the PR."
    ),
    '  Or hide it: customer-registry-sync hide --linear-id lin_hooli --reason "<why>"',
]


def _notion_entries(tmp_path, *recs):
    """Dataset records read back from files `write_dataset` wrote."""
    write_dataset(list(recs), tmp_path)
    return load_datasets(tmp_path, DatasetConfig())


def _acme_rec(**kw):
    return CustomerRecord(
        **{
            "canonical_name": "Acme",
            "domains": ["acme.example.test"],
            "classification": "real",
            "arr": 1.0,
            **kw,
        }
    )


def test_orphan_fails_with_exact_lines():
    hooli = _node("lin_hooli", "Hooli", domains=["hooli.example.test"])
    res = run_check([], [_state_from(hooli)], [hooli], entries=[])
    assert res.code == EXIT_DRIFT and res.orphans == [hooli]
    assert not res.drift.any()
    assert orphan_lines(hooli) == _ORPHAN_HOOLI


def test_orphan_is_a_live_customer_on_an_identity_with_no_record(tmp_path):
    table = _store(
        tmp_path,
        ("hooli", ["Hooli"], ["hooli.example.test"], {"linear": "lin_hooli"}),
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
    )
    entries = _notion_entries(tmp_path, _acme_rec())
    hooli = _node("lin_hooli", "Hooli", domains=["hooli.example.test"], xids=["hooli"])
    acme = _node("lin_1", "Acme [Active]", domains=["acme.example.test"], xids=["acme"])
    nobody = _node("lin_new", "Newco", domains=["newco.example.org"])
    assert orphans([hooli, acme, nobody], table, entries) == [hooli, nobody]


def test_record_with_no_identity_makes_its_live_customer_no_orphan(tmp_path):
    # A record that resolves to nothing gets an implicit identity
    entries = _notion_entries(tmp_path, _acme_rec())
    acme = _node("lin_1", "Acme [Active]", domains=["acme.example.test"], xids=["acme"])
    assert orphans([acme], load_identities(tmp_path / IDENTITIES_FILE), entries) == []


def test_hidden_is_not_an_orphan(tmp_path):
    hid, table = _hidden_own(tmp_path)
    own = _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    res = run_check([], [], [own], table, hidden=hid, entries=[])
    assert res.orphans == [] and res.code == EXIT_OK
    # control: without the hidden entry the same customer is an orphan
    assert run_check([], [], [own], table, entries=[]).orphans == [own]


def test_live_only_revenue_tier_and_domain_are_planned_clears(tmp_path):
    """The registry owns every written field: a live value it lacks is a planned removal
    or clear, never an untracked report, and the check passes."""
    table = _store(tmp_path, ("acme", ["Acme"], [], {"notion": "acme"}))
    entries = _notion_entries(
        tmp_path, CustomerRecord(canonical_name="Acme", classification="real")
    )
    [d] = merge_datasets(entries, table, [NOTION]).rows
    assert (d.domains, d.revenue, d.tier) == ([], None, None)
    lv = _node(
        "lin_1", d.name, domains=["acme.example.test"], revenue=7, tier="Tier 2", xids=["acme"]
    )
    res = run_check([d], [_state_from(lv)], [lv], table, entries=entries)
    assert res.code == EXIT_OK and res.untracked == [] and res.stale == []
    assert res.plan.changes["lin_1"] == [
        FieldChange("domains", "acme.example.test", None, "remove", retire=True),
        FieldChange("revenue", 7, None, "clear", retire=True),
        FieldChange("tier", "Tier 2", None, "clear", retire=True),
    ]


def test_known_duplicate_reported_and_passes(tmp_path):
    entries = _notion_entries(tmp_path, _acme_rec())
    [d] = merge_datasets(entries, load_identities(tmp_path / IDENTITIES_FILE), [NOTION]).rows
    main = _node("lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=["acme"])
    twin = _node("lin_2", "Acme Inc", domains=["acme.example.test"], revenue=1)
    res = run_check([d], [_state_from(main), _state_from(twin)], [main, twin], entries=entries)
    assert res.code == EXIT_OK
    assert [(cid, lv.id) for cid, lv in res.plan.known_duplicates] == [("acme", "lin_2")]
    assert res.orphans == []


def test_unused_identity_reported(tmp_path):
    table = _store(
        tmp_path,
        ("acme", ["Acme"], ["acme.example.test", "OLD.example.org"], {"notion": "acme"}),
        ("ghost", ["Ghost"], ["ghost.example.org"], {"notion": "ghost"}),
    )
    entries = _notion_entries(tmp_path, _acme_rec())
    [d] = merge_datasets(entries, table, [NOTION]).rows
    lv = _node("lin_1", d.name, domains=["acme.example.test", "old.example.org"], xids=["acme"])
    # a Linear customer still holds old.example.org (case-insensitively): only the ghost is unused
    assert unused_identities(table, entries, [lv]) == ["unused identity: ghost"]
    lv = _node("lin_1", d.name, domains=["acme.example.test"], xids=["acme"])
    assert unused_identities(table, entries, [lv]) == [
        "unused identity domain: acme: OLD.example.org",
        "unused identity: ghost",
    ]
    res = run_check([d], [_state_from(lv)], [lv], table, entries=entries)
    assert res.code == EXIT_OK
    assert "unused identity: ghost" in res.reports
    assert "unused identity domain: acme: OLD.example.org" in res.reports


def test_record_with_no_identity_is_reported_and_passes(tmp_path):
    entries = _notion_entries(tmp_path, _acme_rec())
    [d] = merge_datasets(entries, load_identities(tmp_path / IDENTITIES_FILE), [NOTION]).rows
    lv = _node("lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=["acme"])
    res = run_check([d], [_state_from(lv)], [lv], entries=entries)
    assert res.code == EXIT_OK
    assert "no identity: Acme: run ingest" in res.reports


def test_live_resolving_to_two_identities_fails(tmp_path):
    table = _store(
        tmp_path,
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
        ("beta", ["Beta"], ["beta.example.org"], {"notion": "beta"}),
    )
    entries = _notion_entries(
        tmp_path,
        _acme_rec(),
        CustomerRecord(canonical_name="Beta", domains=["beta.example.org"], classification="real"),
    )
    desired = merge_datasets(entries, table, [NOTION]).rows
    lv = _node(
        "lin_1", "Acme [Active]", domains=["acme.example.test", "beta.example.org"], xids=["acme"]
    )
    res = run_check(desired, [_state_from(lv)], [lv], table, entries=entries)
    assert res.code == EXIT_DRIFT
    [msg] = [f for f in res.failures if "resolves to two identities" in f]
    assert "Acme [Active]" in msg and "lin_1" in msg and "acme" in msg and "beta" in msg
    assert "identities.yaml" in msg


_FOUND_YAML = """\
datasets: [notion, linear-found]
linear_status_classification:
  Active: real
  Prospect: prospect
  Churned: churned
"""


def test_linear_edit_to_stub_passes_check(tmp_path, monkeypatch, capsys, run_cli):
    (tmp_path / "datasets.yaml").write_text(_FOUND_YAML)
    write_dataset([], tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    stamped = _node(
        "lin_hooli", "Hooli", domains=["hooli.example.test"], revenue=None, xids=["hooli"]
    )
    write_state(reconcile_mirror([stamped], LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    args = ["--out-dir", str(tmp_path), "--no-input"]
    assert run_cli(["ingest", "--only", "linear-found", *args]) == 0
    assert run_cli(["generate", *args]) == 0
    # a person renames the customer in Linear and sets revenue; the mirror is refreshed
    now = _node(
        "lin_hooli",
        "Hooli Inc",
        domains=["hooli.example.test", "hooli.example.org"],
        revenue=40,
        status="Prospect",
        xids=["hooli"],
        updated="2026-10-09T00:00:00.000Z",
    )
    write_state(reconcile_mirror([now], LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([now]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("IN SYNC")
    assert "UNTRACKED" not in out and "orphan:" not in out


# ---- linear-overrides.yaml ----


def _regenerate(tmp_path):
    """`generate` as the CLI runs it: linear/ and the CSVs, with the manifest."""
    gen = generate_from_dir(tmp_path)
    write_desired(gen.rows, tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")


def _with_overrides(tmp_path, text):
    (tmp_path / "linear-overrides.yaml").write_text(text)
    _regenerate(tmp_path)


def test_check_pending_prints_both_resolutions(tmp_path, monkeypatch, capsys, run_cli):
    _, lv = _generated_tree(tmp_path)
    _with_overrides(
        tmp_path, "customers:\n  acme:\n    pending: {revenue: 150000, domains_add: [acme.dev]}\n"
    )
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "PENDING: acme: revenue = 150000 (edited in Linear)" in out
    assert "keep Linear's value: move it to customers.acme.pin.revenue" in out
    assert "PENDING: acme: domains_add = [acme.dev] (edited in Linear)" in out
    assert "keep Linear's value: move it to customers.acme.domains.add" in out
    assert (
        "upstream wins: delete customers.acme.pending.revenue; "
        "the next deploy writes the registry value"
    ) in out
    assert "upstream wins: delete customers.acme.pending.domains_add" in out
    assert out.rstrip().splitlines()[-1] == (
        "check FAILED: see the ERROR, DRIFT, PENDING, OVERRIDE and OFFLINE lines above"
    )


def _stub_tree(tmp_path, run_cli):
    """A `linear-found` stub built by the real CLI from the mirror; returns its live row."""
    (tmp_path / "datasets.yaml").write_text(_FOUND_YAML)
    write_dataset([], tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    hooli = _node("lin_hooli", "Hooli", domains=["hooli.example.test"], revenue=3, xids=["hooli"])
    write_state(reconcile_mirror([hooli], LINEAR_SCHEMA), tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    args = ["--out-dir", str(tmp_path), "--no-input"]
    assert run_cli(["ingest", "--only", "linear-found", *args]) == 0
    assert run_cli(["generate", *args]) == 0
    return hooli


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("customers:\n  acme:\n    pin: {status: null}\n", "status cannot be pinned empty"),
        (
            "customers:\n  acme:\n    pin: {external_ids: [x]}\n",
            "external_ids can never be pinned",
        ),
        (
            "customers:\n  acme:\n    domains: {remove: [nope.example.test]}\n",
            "acme: domains.remove nope.example.test: upstream does not carry it",
        ),
        ("customers:\n  ghost:\n    pin: {revenue: 5}\n", "ghost: no identity"),
        (
            "customers:\n  hooli:\n    pin: {revenue: 5}\n",
            "hooli: pin does not apply to a Linear-owned stub",
        ),
        ("customers:\n  acme:\n    pin: {revenue: lots}\n", "revenue must be an integer"),
    ],
)
def test_check_override_failures(tmp_path, monkeypatch, capsys, run_cli, text, message):
    if "hooli" in text:
        live = [_stub_tree(tmp_path, run_cli)]
        (tmp_path / "linear-overrides.yaml").write_text(text)
    else:
        _, lv = _generated_tree(tmp_path)
        live = [lv]
        (tmp_path / "linear-overrides.yaml").write_text(text)
        if "null" not in text and "external_ids" not in text:
            _regenerate(tmp_path)
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake(live))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if message in x]
    assert line.lstrip().startswith("OVERRIDE: ")


def test_check_reports_redundant_pin_and_deleted(tmp_path, monkeypatch, capsys, run_cli):
    write_dataset(
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
            ),
            CustomerRecord(
                canonical_name="Gone", domains=["gone.example.test"], classification="real"
            ),
        ],
        tmp_path,
    )
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    _with_overrides(
        tmp_path,
        "customers:\n"
        "  acme:\n    pin: {revenue: 1}\n"
        "  gone:\n    deleted: {linear_id: lin_gone, confirmed: 2026-10-28}\n",
    )
    [desired] = generate_from_dir(tmp_path).rows
    lv = LiveCustomer(
        id="lin_1",
        name=desired.name,
        domains=desired.domains,
        revenue=desired.revenue,
        tier=desired.tier,
        status=desired.status,
        external_ids=desired.external_ids,
        updated_at="2026-10-01T00:00:00.000Z",
    )
    write_state([_state_from(lv)], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "NOTE: acme: pin revenue equals upstream (redundant)" in out
    assert "NOTE: gone: deleted at Linear (lin_gone, 2026-10-28): not recreated" in out
    assert out.rstrip().endswith("IN SYNC")


def test_malformed_overrides_check_exits_3_deploy_exits_2(tmp_path, monkeypatch, capsys, run_cli):
    _, lv = _generated_tree(tmp_path)
    (tmp_path / "linear-overrides.yaml").write_text("customers: [acme]\n")
    reads = []

    class _Counting(_Fake):
        def list_customers(self):
            reads.append(1)
            return super().list_customers()

    monkeypatch.setattr(cli, "_linear_client", lambda: _Counting([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "OVERRIDE: linear-overrides.yaml:1: 'customers' must be a mapping" in out
    assert reads == []  # Linear is never read
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 2
    assert "linear-overrides.yaml:1: 'customers' must be a mapping" in capsys.readouterr().err
    assert reads == []


def test_retire_file_present_refuses(tmp_path, monkeypatch, capsys, run_cli):
    _, lv = _generated_tree(tmp_path)
    (tmp_path / "linear-retire.yaml").write_text("acme:\n  tier: Tier 1\n")
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    for command in ("check", "deploy"):
        assert run_cli([command, "--out-dir", str(tmp_path)]) == 2
        err = capsys.readouterr().err
        assert "ERROR: linear-retire.yaml is no longer read by v0.3.0: run adopt-live" in err
