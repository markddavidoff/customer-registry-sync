from customer_registry_sync import cli
from customer_registry_sync.check import EXIT_DRIFT, EXIT_OK, detect_drift, run_check
from customer_registry_sync.deploy import live_to_linear
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of
from customer_registry_sync.writers import write_desired, write_state


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
    code, drift, plan = run_check([desired], [_state_from(lv)], [lv], {})
    assert code == EXIT_OK and not drift.any() and plan.unchanged
    code, drift, _ = run_check(
        [desired], [_state_from(lv)], [_live("lin_1", "Acme", revenue=9)], {}
    )
    assert code == EXIT_DRIFT and drift.changed


class _Fake:
    def __init__(self, live):
        self.live = live

    def list_customers(self):
        return list(self.live)


def test_cli_check_exit_3_on_drift_and_prints_classes(tmp_path, monkeypatch, capsys, make_config):
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
    assert cli.main(["--config", str(make_config()), "check", "--out-dir", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "DRIFT changed: Acme" in out and "DRIFT added: Newco" in out
