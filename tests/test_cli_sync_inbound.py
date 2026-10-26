from customer_registry_sync import cli
from customer_registry_sync.linear_client import LiveCustomer


class FakeClient:
    def __init__(self, live):
        self._live = live

    def list_customers(self):
        return list(self._live)


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
