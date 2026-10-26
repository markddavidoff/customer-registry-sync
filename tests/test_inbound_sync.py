from customer_registry_sync.check import detect_drift
from customer_registry_sync.inbound_sync import reconcile_inbound
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced, content_hash_of


def live(**kw) -> LiveCustomer:
    base = dict(
        id="lin-1",
        name="Acme Robotics",
        domains=["acmerobotics.example.test"],
        revenue=250000,
        tier="Tier 1",
        status="Active",
        external_ids=[],
        updated_at="2026-10-07T00:00:00.000Z",
    )
    base.update(kw)
    return LiveCustomer(**base)


def test_match_by_external_id_updates_and_refreshes_linear_synced():
    existing = [
        LinearCustomer(
            name="Acme Robotics Old",
            domains=["acmerobotics.example.test"],
            external_ids=["acmerobotics"],
            linear_synced=LinearSynced("stale", "old", "oldhash"),
        )
    ]
    [out] = reconcile_inbound([live(external_ids=["acmerobotics"])], existing)
    assert out.name == "Acme Robotics"  # mirror reflects LIVE
    assert out.external_ids == ["acmerobotics"]  # our slug preserved
    assert out.linear_synced.linear_id == "lin-1"
    assert out.linear_synced.updated_at == "2026-10-07T00:00:00.000Z"
    assert out.linear_synced.content_hash == content_hash_of(out)


def test_domain_first_then_name_keying_and_no_false_merge():
    existing = [
        LinearCustomer(
            name="Hooli Casinos",
            domains=["hoolicasinos.example.test"],
            external_ids=["hoolicasinos"],
        )
    ]
    out = reconcile_inbound(
        [
            live(id="l1", name="Hoolis", domains=[], revenue=None, tier=None, external_ids=[]),
            live(
                id="l2",
                name="Hooli Casinos",
                domains=["hoolicasinos.example.test"],
                revenue=None,
                tier=None,
            ),
        ],
        existing,
    )
    by_name = {o.name: o for o in out}
    # "Hooli Casinos" recovers our slug via domain; "Hoolis" (no domain, different name) does not
    assert by_name["Hooli Casinos"].external_ids == ["hoolicasinos"]
    assert by_name["Hoolis"].external_ids == []


def test_unmatched_live_emitted_as_new_mirror_entry():
    out = reconcile_inbound(
        [live(id="new", name="New Co", domains=["new.com"], external_ids=[])], []
    )
    assert len(out) == 1
    assert out[0].name == "New Co"
    assert out[0].linear_synced.linear_id == "new"


def test_idempotent_on_rerun():
    live_list = [
        live(external_ids=["acmerobotics"]),
        live(id="l2", name="Beta", domains=["beta.example.test"]),
    ]
    out1 = reconcile_inbound(live_list, [])
    out2 = reconcile_inbound(live_list, out1)
    assert out1 == out2


def test_empty_linear_status_is_not_mirrored_as_active():
    """The mirror row hashes the same way deploy and drift detection hash live, so
    a customer with no status is mirrored with none and is not drift."""
    lv = live(id="lin_n", name="Nostatus Co", status="")
    [row] = reconcile_inbound([lv], [])
    assert row.status is None
    assert not detect_drift([row], [lv]).any()
