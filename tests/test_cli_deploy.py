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
        sent = {
            k: v for k, v in f.items() if k in ("name", "domains", "revenue", "external_ids")
        }
        lv = replace(cur, **sent, updated_at="2026-10-03T00:00:00.000Z")
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


def test_deploy_dry_run_returns_3_on_abort(tmp_path, monkeypatch, make_config):
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
    cfg = make_config()
    assert cli.main(["--config", str(cfg), "deploy", "--out-dir", str(tmp_path)]) == 3


def test_deploy_apply_refuses_prod_dir_without_prod_flag(tmp_path, monkeypatch, make_config):
    cfg = make_config(data_dir=tmp_path)
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
    assert cli.main(["--config", str(cfg), "deploy", "--apply", "--out-dir", str(tmp_path)]) == 2
    assert fake.created == []  # nothing written


def test_apply_prod_guard_resolves_symlinked_dir(tmp_path, monkeypatch, make_config):
    real = tmp_path / "customers"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = make_config(data_dir=real)
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
    assert cli.main(["--config", str(cfg), "deploy", "--apply", "--out-dir", str(link)]) == 2
    assert fake.created == []


def test_deploy_apply_writes_and_refreshes_state(tmp_path, monkeypatch, make_config):
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
    cfg = make_config()
    assert cli.main(["--config", str(cfg), "deploy", "--apply", "--out-dir", str(tmp_path)]) == 0
    assert len(fake.updated) == 1
    [st] = load_state(tmp_path)
    assert st.revenue == 5 and st.linear_synced.updated_at == "2026-10-03T00:00:00.000Z"


def test_scope_no_match_is_empty_plan(tmp_path, monkeypatch, make_config):
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
    cfg = make_config()
    argv = [
        "--config",
        str(cfg),
        "deploy",
        "--apply",
        "--out-dir",
        str(tmp_path),
        "--scope",
        "zzz-nomatch-",
    ]
    assert cli.main(argv) == 0
    assert fake.updated == [] and fake.created == []
