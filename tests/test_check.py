from dataclasses import replace

from customer_registry_sync import cli, deploy
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
from customer_registry_sync.curated import CuratedFields
from customer_registry_sync.datasets import (
    LINEAR_FOUND,
    NOTION,
    DatasetConfig,
    DatasetRecord,
    dataset_dir,
    load_datasets,
)
from customer_registry_sync.deploy import live_to_linear
from customer_registry_sync.fieldplan import FieldChange
from customer_registry_sync.generate import generate_from_dir, merge_datasets
from customer_registry_sync.identity import IDENTITIES_FILE, load_identities
from customer_registry_sync.identity_store import IdentityStore
from customer_registry_sync.ignore import IGNORE_FILE, load_ignore
from customer_registry_sync.inbound_sync import reconcile_inbound
from customer_registry_sync.linear_client import LiveCustomer, parse_customer_node
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.manifest import MANIFEST, write_manifest
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.writers import (
    load_state,
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
    s = live_to_linear(lv)
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


def test_check_reports_untracked_and_passes():
    # spec §6 moved untracked values from fail to report (D4)
    lv = _live("lin_1", "Acme")  # live carries acme.example.test; the registry row has no domains
    res = run_check([_desired_acme(domains=[])], [_state_from(lv)], [lv])
    assert res.code == EXIT_OK and not res.drift.any() and not res.plan.aborts
    assert [(u.field, u.value) for u in res.untracked] == [("domains", "acme.example.test")]


def test_check_exits_3_on_stale_retire():
    lv = _live("lin_1", "Acme")
    retire = {"acme": CuratedFields(domains=["gone.example.org"])}
    res = run_check([_desired_acme()], [_state_from(lv)], [lv], retire=retire)
    assert res.code == EXIT_DRIFT and res.stale == ["acme: domains gone.example.org not live"]
    assert not res.drift.any() and not res.untracked


def test_check_exits_3_on_unacked_destructive(monkeypatch):
    lv = _live("lin_1", "Acme")
    bad = FieldChange("revenue", 1, None, "clear")  # retire=False: a planner bug
    monkeypatch.setattr(deploy, "plan_row", lambda d, live, retire: (d, [bad]))
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
        == "check FAILED: see the ERROR, DRIFT and OFFLINE lines above"
    )
    assert "run sync-inbound, resolve, re-run" not in out


def test_cli_check_prints_untracked_and_stale_retire(tmp_path, monkeypatch, capsys, run_cli):
    lv = _live("lin_1", "Acme")
    write_desired([_desired_acme(domains=[], revenue=None)], tmp_path)
    write_state([_state_from(lv)], tmp_path)
    (tmp_path / "linear-retire.yaml").write_text(
        "acme:\n  domains: [gone.example.org]\nghost:\n  tier: Tier 1\n"
    )
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([lv]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "UNTRACKED: Acme: domains += acme.example.test" in out
    assert "UNTRACKED: Acme: revenue = 1" in out
    assert out.index("DeployPlan:") < out.index("UNTRACKED: Acme: domains += acme.example.test")
    assert "STALE RETIRE: acme: domains gone.example.org not live" in out
    assert "STALE RETIRE: ghost: no registry row" in out


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


def test_offline_errors_on_invalid_ignore_file(tmp_path):
    _generated_tree(tmp_path)
    (tmp_path / IGNORE_FILE).write_text("customers:\n  - linear_id: lin_a\n")
    assert offline_errors(tmp_path) == ["linear-ignore.yaml:2: missing reason"]


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
    # two live customers on one identity, neither stamped nor recorded (R7)
    table = _store(tmp_path, ("acme", ["Acme"], ["acme.example.test"], {}))
    a, b = (replace(_live(lid, "Acme"), external_ids=[]) for lid in ("lin_a", "lin_b"))
    res = run_check([_desired_acme()], [_state_from(a), _state_from(b)], [a, b], table)
    assert res.code == EXIT_DRIFT and res.plan.errors
    assert not res.drift.any() and not res.untracked and not res.stale


def test_identity_offline_errors_include_table_errors(tmp_path):
    table = _store(
        tmp_path, ("a", ["A"], ["x.example.test"], {}), ("b", ["B"], ["y.example.test"], {})
    )
    table.entries["b"].domains.append(
        "x.example.test"
    )  # the store refuses this; a human edit does not
    assert identity_offline_errors([], table) == [
        "domain 'x.example.test' is on two identities: a, b"
    ]


# ---- the one managed/ignored partition (spec §4a) ----

_IGNORE_OWN = (
    "customers:\n  - linear_id: lin_own\n    name: Pied Piper\n    reason: our own company\n"
)


def _ignore_list(tmp_path, text=_IGNORE_OWN):
    (tmp_path / IGNORE_FILE).write_text(text)
    return load_ignore(tmp_path / IGNORE_FILE)


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


def test_ignored_not_in_drift(tmp_path):
    ign = _ignore_list(tmp_path)
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
    res = run_check([_desired_acme()], state, [acme, own_now], ignore=ign)
    assert not res.drift.any() and res.code == EXIT_OK
    assert [lv.id for lv in res.ignored] == ["lin_own"]
    # an ignored customer deleted from Linear is not "deleted" drift either
    res = run_check([_desired_acme()], state, [acme], ignore=ign)
    assert not res.drift.any()
    # control: without the ignore entry the same edit is drift
    assert run_check([_desired_acme()], state, [acme, own_now]).drift.changed


def test_ignored_not_in_plan(tmp_path):
    ign = _ignore_list(tmp_path)
    acme, own = _managed_acme(), _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    state = [_state_from(acme), _state_from(own)]
    res = run_check([_desired_acme()], state, [acme, own], ignore=ign)
    assert res.plan.orphans == [] and not res.plan.updates and not res.plan.creates
    assert [d.name for d in res.plan.unchanged] == ["Acme"]
    assert run_check([_desired_acme()], state, [acme, own]).plan.orphans == [own]


def test_ignored_not_untracked(tmp_path):
    # the ignored customer carries the canonical id too and comes first, so an
    # unpartitioned planner would pair it and report its extra domain
    ign = _ignore_list(tmp_path)
    acme = _managed_acme()
    own = _node(
        "lin_own",
        "Pied Piper",
        domains=["acme.example.test", "piedpiper.example.test"],
        xids=["acme"],
    )
    res = run_check([_desired_acme()], [], [own, acme], ignore=ign)
    assert res.untracked == []
    assert [d.name for d in res.plan.unchanged] == ["Acme"]
    # it resolves to Acme's identity, so it is an ignore conflict instead
    assert res.failures == [
        (
            "desired record 'Acme' shares source id 'acme' with ignored Linear customer 'Pied Piper' "
            "(linear-ignore.yaml:2). Remove that entry or change Acme."
        )
    ]
    assert res.code == EXIT_DRIFT
    # control: unpartitioned, both carry the canonical id, so the planner refuses
    assert run_check([_desired_acme()], [], [own, acme]).plan.errors == [_TWO_STAMPED]


def test_ignored_not_in_stale_retire(tmp_path):
    ign = _ignore_list(tmp_path)
    acme = _managed_acme()
    own = _node(
        "lin_own", "Pied Piper", domains=["acme.example.test", "old.example.org"], xids=["acme"]
    )
    retire = {"acme": CuratedFields(domains=["old.example.org"])}
    res = run_check([_desired_acme()], [], [own, acme], retire=retire, ignore=ign)
    assert res.stale == ["acme: domains old.example.org not live"]  # only the ignored row has it
    # control: unpartitioned, both carry the canonical id, so the planner refuses
    unpartitioned = run_check([_desired_acme()], [], [own, acme], retire=retire)
    assert unpartitioned.plan.errors == [_TWO_STAMPED]
    assert unpartitioned.code == EXIT_DRIFT


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
    """R12: a stub whose Linear status is not in the table is written with
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
        'or ignore it: customer-registry-sync ignore --linear-id lin_p --reason "<why>"'
    ) in offline_errors(tmp_path)


# ---- orphans, failure lines and reports (spec §6, D4, D6) ----

_ORPHAN_HOOLI = [
    "orphan: Linear customer 'Hooli' (lin_hooli) is in no dataset.",
    (
        "  Add it: dispatch the registry-sync workflow with mode=refresh (or run sync-inbound, "
        "ingest --only linear-found, generate) and merge the PR."
    ),
    '  Or ignore it: customer-registry-sync ignore --linear-id lin_hooli --reason "<why>"',
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
    # R3: a record that resolves to nothing gets an implicit identity
    entries = _notion_entries(tmp_path, _acme_rec())
    acme = _node("lin_1", "Acme [Active]", domains=["acme.example.test"], xids=["acme"])
    assert orphans([acme], load_identities(tmp_path / IDENTITIES_FILE), entries) == []


def test_ignored_is_not_an_orphan(tmp_path):
    ign = _ignore_list(tmp_path)
    own = _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    res = run_check([], [], [own], ignore=ign, entries=[])
    assert res.orphans == [] and res.code == EXIT_OK
    # control: without the ignore entry the same customer is an orphan
    assert run_check([], [], [own], entries=[]).orphans == [own]


def test_untracked_revenue_tier_and_domain_pass_with_report(tmp_path):
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
    assert res.code == EXIT_OK
    assert {(u.field, u.value) for u in res.untracked} == {
        ("domains", "acme.example.test"),
        ("revenue", 7),
        ("tier", "Tier 2"),
    }


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
    write_state(reconcile_inbound([stamped], []), tmp_path)
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
    write_state(reconcile_inbound([now], load_state(tmp_path)), tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")
    monkeypatch.setattr(cli, "_linear_client", lambda: _Fake([now]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("IN SYNC")
    assert "UNTRACKED" not in out and "orphan:" not in out
