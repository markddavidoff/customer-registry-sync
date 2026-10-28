from dataclasses import replace

from customer_registry_sync import cli
from customer_registry_sync.deploy import live_to_linear
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.writers import load_state, write_desired, write_state


class FakeClient:
    def __init__(self, live):
        self.live = list(live)
        self.created = []
        self.updated = []

    def list_customers(self):
        return list(self.live)

    def status_ids(self):
        return {"Active": "st_active", "Prospect": "st_p", "Churned": "st_c"}

    def tier_ids(self):
        return {}

    def get_customer(self, cid):
        return next((c for c in self.live if c.id == cid), None)

    def create_customer(self, **f):
        lv = LiveCustomer(
            id=f"lin_new{len(self.created)}",
            name=f["name"],
            domains=f["domains"],
            revenue=f.get("revenue"),
            tier=None,
            status="Active",
            external_ids=f["external_ids"],
            updated_at="2026-10-03T00:00:00.000Z",
        )
        self.created.append(lv)
        self.live.append(lv)
        return lv

    def update_customer(self, cid, *, retire=False, **f):
        # apply only the fields sent (apply_deploy sends the planned changes only)
        cur = self.get_customer(cid)
        lv = replace(
            cur,
            **{k: v for k, v in f.items() if k in ("name", "domains", "revenue", "external_ids")},
            updated_at="2026-10-03T00:00:00.000Z",
        )
        self.live[self.live.index(cur)] = lv
        self.updated.append(lv)
        return lv


def _live(lid, name, updated="2026-10-01T00:00:00.000Z", revenue=1, xids=("acme",)):
    return LiveCustomer(
        id=lid,
        name=name,
        domains=["acme.example.test"],
        revenue=revenue,
        tier=None,
        status="Active",
        external_ids=list(xids),
        updated_at=updated,
    )


def _state_from(lv):
    s = live_to_linear(lv)
    s.linear_synced = LinearSynced(lv.id, lv.updated_at, content_hash_of(s))
    return s


def _seed(tmp_path, desired, state):
    write_desired(desired, tmp_path)
    write_state(state, tmp_path)


def test_deploy_dry_run_returns_3_on_abort(tmp_path, monkeypatch, run_cli):
    recorded = _live("lin_1", "Acme")
    live_now = _live("lin_1", "Acme", updated="2026-10-02T00:00:00.000Z", revenue=99)
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [_state_from(recorded)],
    )
    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient([live_now]))
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 3


def test_deploy_apply_refuses_prod_dir_without_prod_flag(tmp_path, monkeypatch, run_cli):
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [],
    )
    fake = FakeClient([])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)], data_dir=tmp_path) == 2
    assert fake.created == []  # nothing written


def test_apply_prod_guard_resolves_symlinked_dir(tmp_path, monkeypatch, run_cli):
    real = tmp_path / "customers"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _seed(
        real,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [],
    )
    fake = FakeClient([])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert (
        run_cli(["deploy", "--apply", "--out-dir", str(link)], data_dir=real) == 2
        and fake.created == []
    )


def test_deploy_apply_writes_and_refreshes_state(tmp_path, monkeypatch, run_cli):
    live = _live("lin_1", "Acme")
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [_state_from(live)],
    )
    fake = FakeClient([live])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 0
    assert len(fake.updated) == 1
    [st] = load_state(tmp_path)
    assert st.revenue == 5 and st.linear_synced.updated_at == "2026-10-03T00:00:00.000Z"


def test_scope_no_match_is_empty_plan(tmp_path, monkeypatch, run_cli):
    live = _live("lin_1", "Acme")
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [_state_from(live)],
    )
    fake = FakeClient([live])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert (
        run_cli(["deploy", "--apply", "--out-dir", str(tmp_path), "--scope", "zzz-nomatch-"]) == 0
    )
    assert fake.updated == [] and fake.created == []


def _seed_acme(tmp_path, recording_linear):
    live = recording_linear.node(
        "lin_1", "Acme", domains=["acme.example.test"], revenue=1, xids=["acme"]
    )
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [_state_from(live)],
    )
    return recording_linear([live])


def test_prod_apply_needs_break_glass_outside_actions(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = _seed_acme(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert (
        run_cli(["deploy", "--apply", "--prod", "--out-dir", str(tmp_path)], data_dir=tmp_path) == 2
    )
    assert "--apply --prod outside GitHub Actions needs --break-glass" in capsys.readouterr().err
    assert fake.updates() == [] and not (tmp_path / "linear-snapshots").exists()
    # break-glass: warns, still snapshots, writes
    assert (
        run_cli(
            ["deploy", "--apply", "--prod", "--break-glass", "--out-dir", str(tmp_path)],
            data_dir=tmp_path,
        )
        == 0
    )
    out = capsys.readouterr()
    assert "WARNING" in out.err and "break-glass" in out.err
    assert len(fake.updates()) == 1 and list((tmp_path / "linear-snapshots").glob("*.yaml"))


def test_prod_apply_allowed_in_actions(tmp_path, monkeypatch, capsys, recording_linear, run_cli):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _seed_acme(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert (
        run_cli(
            ["deploy", "--apply", "--prod", "--no-input", "--out-dir", str(tmp_path)],
            data_dir=tmp_path,
        )
        == 0
    )
    assert fake.rows["lin_1"].revenue == 5
    [snap] = (tmp_path / "linear-snapshots").glob("*.yaml")
    out = capsys.readouterr().out
    assert f"snapshot: {snap}" in out
    assert f"restore: customer-registry-sync restore --from {snap} --prod" in out
    [st] = load_state(tmp_path)
    assert st.revenue == 5 and st.linear_synced.updated_at == fake.rows["lin_1"].updated_at


def test_deploy_refuses_unacked_destructive(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    fake = _seed_acme(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    real = cli.plan_deploy

    def buggy_planner(*a, **kw):  # the planner never emits these; simulate the bug
        plan = real(*a, **kw)
        plan.unacked.append("Acme: revenue clear without retire")
        return plan

    monkeypatch.setattr(cli, "plan_deploy", buggy_planner)
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 3
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 3
    assert "UNACKNOWLEDGED DESTRUCTIVE: Acme: revenue clear" in capsys.readouterr().out
    assert fake.updates() == [] and not (tmp_path / "linear-snapshots").exists()


def test_deploy_refuses_on_planner_errors_and_loads_identities(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    from customer_registry_sync.identity import IDENTITIES_FILE
    from customer_registry_sync.identity import IdentityStore

    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    store.add_identity(
        "acme", names=["Acme", "Acme Inc"], domains=["acme.example.test"], sources={}
    )
    store.save()
    a = recording_linear.node("lin_a", "Acme Corp", domains=["acme.example.test"], revenue=1)
    b = recording_linear.node("lin_b", "Acme Inc", revenue=1)  # pairs only by the identity name
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [],
    )
    fake = recording_linear([a, b])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 3
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "ERROR: known duplicates for acme" in out and "REFUSED" in out
    assert [c for c in fake.calls if c[0] != "get"] == []
    assert not (tmp_path / "linear-snapshots").exists()


def test_deploy_passes_retire_file_to_planner(tmp_path, monkeypatch, recording_linear, run_cli):
    from customer_registry_sync.curated import RETIRE_FILE

    live = recording_linear.node(
        "lin_1", "Acme", domains=["acme.example.test", "old.example.org"], revenue=5, xids=["acme"]
    )
    _seed(
        tmp_path,
        [
            LinearCustomer(
                name="Acme",
                domains=["acme.example.test"],
                revenue=5,
                status="Active",
                external_ids=["acme"],
            )
        ],
        [_state_from(live)],
    )
    (tmp_path / RETIRE_FILE).write_text("acme:\n  domains: [old.example.org]\n")
    fake = recording_linear([live])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 0
    [(_, _, fields, retire, _)] = fake.updates()
    assert (fields, retire) == ({"domains": ["acme.example.test"]}, True)


def test_deploy_apply_reports_verification_failure(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    fake = _seed_acme(tmp_path, recording_linear)
    fake.tamper["lin_1"] = {"tier": "Tier 2"}
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "Acme: tier expected None got Tier 2" in out and "restore: " in out


def test_restore_cli_round_trip_and_break_glass(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _seed_acme(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert (
        run_cli(["deploy", "--apply", "--prod", "--out-dir", str(tmp_path)], data_dir=tmp_path) == 0
    )
    [snap] = (tmp_path / "linear-snapshots").glob("*.yaml")
    monkeypatch.delenv("GITHUB_ACTIONS")
    argv = ["restore", "--from", str(snap), "--out-dir", str(tmp_path), "--prod"]
    assert run_cli(argv, data_dir=tmp_path) == 2 and fake.rows["lin_1"].revenue == 5
    capsys.readouterr()
    assert run_cli([*argv, "--break-glass", "--rows", "acme"], data_dir=tmp_path) == 0
    assert fake.rows["lin_1"].revenue == 1
    assert "restored: 1" in capsys.readouterr().out
    # a second restore finds the row changed since the snapshot's write: skipped, exit 3
    assert run_cli([*argv, "--break-glass"], data_dir=tmp_path) == 3
    assert "SKIPPED: lin_1" in capsys.readouterr().out


def test_restore_stops_on_linear_error_and_reports_partial(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    from customer_registry_sync.linear_client import LinearError
    from customer_registry_sync.snapshot import restore, write_snapshot

    rows = [
        recording_linear.node(
            f"lin_{i}", f"C{i}", domains=[f"c{i}.example.test"], revenue=i, xids=[f"c{i}"]
        )
        for i in (1, 2, 3)
    ]
    snap = write_snapshot(rows, tmp_path / "snaps")
    fake = recording_linear(rows)
    fake.fail["lin_2"] = LinearError("HTTP 502")
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)

    res = restore(snap, fake)
    assert res.restored == ["lin_1"] and res.skipped == []
    assert res.failure == "lin_2: HTTP 502"
    assert [c[1] for c in fake.updates()] == ["lin_1", "lin_2"]  # lin_3 never attempted

    fake2 = recording_linear(rows)
    fake2.fail["lin_2"] = LinearError("HTTP 502")
    monkeypatch.setattr(cli, "_linear_client", lambda: fake2)
    capsys.readouterr()
    assert run_cli(["restore", "--from", str(snap), "--out-dir", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "RESTORED: lin_1" in out and "FAILED: lin_2: HTTP 502" in out
    assert "lin_3" not in out
    assert [c[1] for c in fake2.updates()] == ["lin_1", "lin_2"]


# ---- the ignore list in check and deploy ----

_IGNORE_OWN = (
    "customers:\n  - linear_id: lin_own\n    name: Pied Piper\n    reason: our own company\n"
)


def _generated(tmp_path, recs, *identities):
    """A data dir built by the real producers (identity store, dataset,
    generate); returns the desired rows."""
    from gitops_engine.datasets import dataset_dir
    from gitops_engine.manifest import write_manifest

    from customer_registry_sync.datasets import NOTION
    from customer_registry_sync.generate import generate_from_dir
    from customer_registry_sync.identity import IDENTITIES_FILE
    from customer_registry_sync.identity import IdentityStore
    from customer_registry_sync.writers import write_csvs, write_dataset

    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        store.add_identity(cid, names=names, domains=domains, sources=sources)
    store.save()
    write_dataset(recs, tmp_path)
    write_manifest(dataset_dir(tmp_path, NOTION), "ingest")
    gen = generate_from_dir(tmp_path)
    write_desired(gen.rows, tmp_path)
    write_csvs(gen.winners, tmp_path)
    write_manifest(tmp_path / "linear", "generate")
    return gen.rows


def _mirror(tmp_path, live):
    from gitops_engine.manifest import write_manifest

    write_state([_state_from(lv) for lv in live], tmp_path)
    write_manifest(tmp_path / "linear-state", "sync-inbound")


def test_deploy_refuses_on_ignore_conflict(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    from customer_registry_sync.ignore import IGNORE_FILE
    from customer_registry_sync.schema import CustomerRecord

    _generated(
        tmp_path,
        [
            CustomerRecord(
                canonical_name="Pied Piper Consulting",
                domains=["piedpiper.example.test"],
                classification="real",
                arr=5.0,
            )
        ],
    )
    (tmp_path / IGNORE_FILE).write_text(_IGNORE_OWN)
    own = recording_linear.node(
        "lin_own", "Pied Piper", domains=["piedpiper.example.test"], revenue=1
    )
    _mirror(tmp_path, [own])
    fake = recording_linear([own])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--out-dir", str(tmp_path)]) == 3
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert (
        "ERROR: desired record 'Pied Piper Consulting' shares domain 'piedpiper.example.test' with ignored Linear "
        "customer 'Pied Piper' (linear-ignore.yaml:2). Remove that entry or change Pied Piper Consulting."
        in out
    )
    assert "REFUSED" in out
    assert [c for c in fake.calls if c[0] != "get"] == []
    assert not (tmp_path / "linear-snapshots").exists()


def test_deploy_withholds_domain_held_only_by_ignored_customer(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    from customer_registry_sync.ignore import IGNORE_FILE
    from customer_registry_sync.schema import CustomerRecord

    # the identity lists only acme.example.test, so Pied Piper does not resolve to Acme (no conflict),
    # yet the record also carries piedpiper.example.test, which only the ignored Pied Piper holds
    [d] = _generated(
        tmp_path,
        [
            CustomerRecord(
                canonical_name="Acme",
                domains=["acme.example.test", "piedpiper.example.test"],
                classification="real",
                arr=1.0,
            )
        ],
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
    )
    assert d.domains == ["acme.example.test", "piedpiper.example.test"]
    (tmp_path / IGNORE_FILE).write_text(_IGNORE_OWN)
    acme = recording_linear.node(
        "lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=["acme"]
    )
    own = recording_linear.node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    _mirror(tmp_path, [acme, own])
    fake = recording_linear([acme, own])
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert (
        "WITHHELD: withheld domain 'piedpiper.example.test' for 'Acme [Active]': Linear customer 'Pied Piper' "
        "already holds it"
    ) in out
    assert fake.updates() == [] and fake.rows["lin_1"].domains == ["acme.example.test"]


def _acme_and_own_holder(tmp_path, recording_linear):
    """Acme's record carries piedpiper.example.test, which only the ignored Pied Piper holds; the
    identity lists only acme.example.test, so Pied Piper does not resolve to Acme."""
    from customer_registry_sync.ignore import IGNORE_FILE
    from customer_registry_sync.schema import CustomerRecord

    [d] = _generated(
        tmp_path,
        [
            CustomerRecord(
                canonical_name="Acme",
                domains=["acme.example.test", "piedpiper.example.test"],
                classification="real",
                arr=1.0,
            )
        ],
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
    )
    (tmp_path / IGNORE_FILE).write_text(_IGNORE_OWN)
    acme = recording_linear.node(
        "lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=["acme"]
    )
    own = recording_linear.node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    _mirror(tmp_path, [acme, own])
    return recording_linear([acme, own])


_WITHHELD_OWN = (
    "WITHHELD: withheld domain 'piedpiper.example.test' for 'Acme [Active]': Linear customer "
    "'Pied Piper' already holds it"
)


def test_scoped_deploy_withholds_domain_held_outside_the_scope(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    # the holder is ignored AND outside --scope: it is neither managed nor in the
    # scoped live list, yet its domain still counts as held
    fake = _acme_and_own_holder(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    assert run_cli(["deploy", "--apply", "--out-dir", str(tmp_path), "--scope", "Acme"]) == 0
    assert _WITHHELD_OWN in capsys.readouterr().out
    assert fake.updates() == [] and fake.rows["lin_1"].domains == ["acme.example.test"]


def test_scoped_check_withholds_domain_held_outside_the_scope(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    fake = _acme_and_own_holder(tmp_path, recording_linear)
    monkeypatch.setattr(cli, "_linear_client", lambda: fake)
    run_cli(["check", "--out-dir", str(tmp_path), "--scope", "Acme"])
    out = capsys.readouterr().out
    assert _WITHHELD_OWN in out
    assert "~ Acme [Active]" not in out  # no update planned that would add piedpiper.example.test


def test_check_lists_ignored_customers(tmp_path, monkeypatch, capsys, recording_linear, run_cli):
    from customer_registry_sync.ignore import IGNORE_FILE
    from customer_registry_sync.schema import CustomerRecord

    [d] = _generated(
        tmp_path,
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
            )
        ],
    )
    (tmp_path / IGNORE_FILE).write_text(
        _IGNORE_OWN + "  - linear_id: lin_gone\n    reason: deleted test org\n"
    )
    acme = recording_linear.node(
        "lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=d.external_ids
    )
    own = recording_linear.node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    e2e = recording_linear.node("lin_e2e", "zzz-gitops-test-0000-Acme")
    _mirror(tmp_path, [acme])
    monkeypatch.setattr(cli, "_linear_client", lambda: recording_linear([acme, own, e2e]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0  # stale entries only warn
    out = capsys.readouterr().out
    assert "IGNORED: 'Pied Piper' (lin_own): our own company" in out
    assert (
        "IGNORED: 'zzz-gitops-test-0000-Acme' (lin_e2e): live e2e harness throwaway "
        "records (built in; always present)"
    ) in out
    assert "IGNORED PREFIX: zzz-gitops-test- (built-in)" in out
    assert "stale ignore entry: linear-ignore.yaml:5: lin_gone is not in Linear" in out
    assert "DRIFT added" not in out and "ORPHAN" not in out


def test_check_orphan_lines_print_before_the_plan_and_reports_after(
    tmp_path, monkeypatch, capsys, recording_linear, run_cli
):
    from customer_registry_sync.ignore import IGNORE_FILE
    from customer_registry_sync.schema import CustomerRecord

    [d] = _generated(
        tmp_path,
        [
            CustomerRecord(
                canonical_name="Acme", domains=["acme.example.test"], classification="real", arr=1.0
            )
        ],
        ("acme", ["Acme"], ["acme.example.test"], {"notion": "acme"}),
        ("ghost", ["Ghost"], ["ghost.example.org"], {"notion": "ghost"}),
    )
    acme = recording_linear.node(
        "lin_1", d.name, domains=["acme.example.test"], revenue=1, xids=d.external_ids
    )
    hooli = recording_linear.node("lin_hooli", "Hooli", domains=["hooli.example.test"])
    _mirror(tmp_path, [acme, hooli])
    monkeypatch.setattr(cli, "_linear_client", lambda: recording_linear([acme, hooli]))
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert (
        "  ERROR: orphan: Linear customer 'Hooli' (lin_hooli) is in no dataset.\n"
        "    Add it: dispatch the registry-sync workflow with mode=refresh (or run sync-inbound, "
        "ingest --only linear-found, generate) and merge the PR.\n"
        "    Or ignore it: customer-registry-sync ignore --linear-id lin_hooli "
        '--reason "<why>"\n'
    ) in out
    assert (
        out.index("ERROR: orphan:")
        < out.index("DeployPlan:")
        < out.index("NOTE: unused identity: ghost")
    )
    # a scoped run sees only its own customers: no orphan, and no identity looks unused
    assert run_cli(["check", "--out-dir", str(tmp_path), "--scope", "Acme"]) == 0
    assert "unused identity" not in capsys.readouterr().out
    # the printed ignore command clears the orphan
    (tmp_path / IGNORE_FILE).write_text(
        "customers:\n  - linear_id: lin_hooli\n    name: Hooli\n    reason: not a customer\n"
    )
    assert run_cli(["check", "--out-dir", str(tmp_path)]) == 0
    assert "orphan:" not in capsys.readouterr().out
