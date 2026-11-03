from customer_registry_sync import cli
from customer_registry_sync.linear_client import LiveCustomer


class FakeClient:
    def __init__(self, live):
        self._live = live

    def list_customers(self):
        return list(self._live)

    def get_customer(self, customer_id):
        listed = {lv.id: lv for lv in self._live}
        assert customer_id in listed, f"unexpected get {customer_id}"
        return listed[customer_id]


def _live(name, lid, **kw):
    return LiveCustomer(
        id=lid,
        name=name,
        domains=kw.get("domains", []),
        revenue=kw.get("revenue"),
        tier=None,
        status="Active",
        external_ids=kw.get("external_ids", []),
        updated_at="2026-10-01T00:00:00.000Z",
    )


def test_sync_inbound_writes_state_dir_only(tmp_path, monkeypatch, run_cli):
    monkeypatch.setattr(
        cli,
        "_linear_client",
        lambda: FakeClient([_live("Acme", "lin_1", domains=["acme.example.test"])]),
    )
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "linear-state" / "acme.yaml").exists()
    assert not (tmp_path / "linear").exists()


def test_sync_inbound_scope_preserves_out_of_scope_state(tmp_path, monkeypatch, run_cli):
    from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced
    from customer_registry_sync.writers import load_state, write_state

    keep = LinearCustomer(
        name="Acme", domains=["acme.example.test"], status="Active", external_ids=["acme"]
    )
    keep.linear_synced = LinearSynced("lin_keep", "2026-10-01T00:00:00.000Z", "h")
    stale = LinearCustomer(name="zzz-t-old", status="Active", external_ids=["zzz-t-old"])
    stale.linear_synced = LinearSynced("lin_old", "2026-10-01T00:00:00.000Z", "h")
    write_state([keep, stale], tmp_path)
    live = [
        _live("Acme", "lin_keep", domains=["acme.example.test"]),
        _live("zzz-t-new", "lin_new", external_ids=["zzz-t-new"]),
    ]
    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient(live))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--scope", "zzz-t-"]) == 0
    names = sorted(s.name for s in load_state(tmp_path))
    assert names == ["Acme", "zzz-t-new"]  # Acme untouched, zzz-t-old dropped, zzz-t-new added


def test_cli_sync_inbound_dry_run_writes_nothing(tmp_path, monkeypatch, capsys, run_cli):
    """An edit is detected and counted, but neither the mirror nor the overrides file
    is written."""
    from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced
    from customer_registry_sync.writers import write_desired, write_state

    old = LinearCustomer(
        name="Acme [Active]",
        domains=["acme.example.test"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    write_desired([old], tmp_path)
    old.linear_synced = LinearSynced("lin_1", "2026-10-01T00:00:00.000Z", "h")
    write_state([old], tmp_path)
    before = sorted((p.name, p.read_bytes()) for p in (tmp_path / "linear-state").iterdir())
    edited = _live(
        "Acme [Active]", "lin_1", domains=["acme.example.test"], revenue=7, external_ids=["acme"]
    )
    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient([edited]))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "sync-inbound: 1 mirrored; 0 absorbed, 1 pending" in out
    assert "DRY RUN" in out
    after = sorted((p.name, p.read_bytes()) for p in (tmp_path / "linear-state").iterdir())
    assert after == before
    assert not (tmp_path / "linear-overrides.yaml").exists()


def _recorded_pending(tmp_path, monkeypatch, run_cli):
    """A data dir after a real `sync-inbound` recorded a pending edit: a mirror of Acme
    (lin_1) and a non-empty linear-overrides.yaml."""
    from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced
    from customer_registry_sync.writers import write_desired, write_state

    old = LinearCustomer(
        name="Acme [Active]",
        domains=["acme.example.test"],
        revenue=5,
        status="Active",
        external_ids=["acme"],
    )
    write_desired([old], tmp_path)
    old.linear_synced = LinearSynced("lin_1", "2026-10-01T00:00:00.000Z", "h")
    write_state([old], tmp_path)
    edited = _live(
        "Acme [Active]", "lin_1", domains=["acme.example.test"], revenue=7, external_ids=["acme"]
    )
    monkeypatch.setattr(cli, "_linear_client", lambda: FakeClient([edited]))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--no-input"]) == 0
    assert (tmp_path / "linear-overrides.yaml").read_text().strip()


def _written(tmp_path):
    state = sorted((p.name, p.read_bytes()) for p in (tmp_path / "linear-state").iterdir())
    return state, (tmp_path / "linear-overrides.yaml").read_bytes()


class _ListingFails(FakeClient):
    def list_customers(self):
        from customer_registry_sync.linear_client import LinearError

        raise LinearError("listing failed")


class _GetFails(FakeClient):
    def get_customer(self, customer_id):
        from customer_registry_sync.linear_client import LinearError

        raise LinearError("get failed")


def test_listing_error_exits_2_and_writes_nothing(tmp_path, monkeypatch, capsys, run_cli):
    _recorded_pending(tmp_path, monkeypatch, run_cli)
    before = _written(tmp_path)
    monkeypatch.setattr(cli, "_linear_client", lambda: _ListingFails([]))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--no-input"]) == 2
    assert "listing failed" in capsys.readouterr().err
    assert _written(tmp_path) == before


def test_get_error_exits_2_and_writes_nothing(tmp_path, monkeypatch, capsys, run_cli):
    """Acme is missing from the listing, and the confirming `get` fails."""
    _recorded_pending(tmp_path, monkeypatch, run_cli)
    before = _written(tmp_path)
    monkeypatch.setattr(cli, "_linear_client", lambda: _GetFails([]))
    assert run_cli(["sync-inbound", "--out-dir", str(tmp_path), "--no-input"]) == 2
    assert "get failed" in capsys.readouterr().err
    assert _written(tmp_path) == before
