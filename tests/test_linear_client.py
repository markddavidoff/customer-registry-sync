import io
import json
import urllib.error
from pathlib import Path

import pytest

from customer_registry_sync.linear_client import (
    LinearClient,
    LinearError,
    LinearNotFound,
    LiveCustomer,
    parse_customer_node,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_customer_node_from_fixture():
    data = json.loads((FIX / "linear_customers_response.json").read_text())
    nodes = data["data"]["customers"]["nodes"]
    by_name = {n["name"]: parse_customer_node(n) for n in nodes}
    acme = by_name["Acme Robotics"]
    assert isinstance(acme, LiveCustomer)
    assert acme.domains == [] and acme.revenue is None and acme.external_ids == []
    assert acme.status == "Active" and acme.tier is None
    assert acme.updated_at == "2026-08-12T08:11:45.318Z"
    assert by_name["Globex Labs"].domains == ["globexlabs.example.test"]
    assert by_name["Initrode Systems"].status == "Prospect"
    soylent = by_name["Soylent Metrics"]  # populated revenue / externalIds / tier
    assert soylent.revenue == 24000 and soylent.external_ids == ["soylent-metrics"]
    assert soylent.tier == "Tier 1"


def _pager(pages):
    """Stub gql that returns successive customers-query pages."""
    state = {"i": 0}

    def gql(query, variables=None):
        p = pages[state["i"]]
        state["i"] += 1
        return p

    gql.seen = state
    return gql


def _customers_page(nodes, has_next, cursor=None):
    return {
        "data": {
            "customers": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


def _node(name, **kw):
    base = {
        "id": name.lower(),
        "name": name,
        "domains": [],
        "revenue": None,
        "externalIds": [],
        "updatedAt": "2026-01-01T00:00:00.000Z",
        "status": {"id": "s", "name": "Active"},
        "tier": None,
    }
    base.update(kw)
    return base


def test_list_customers_paginates():
    pages = [
        _customers_page([_node("A")], True, "cur1"),
        _customers_page([_node("B")], False, None),
    ]
    c = LinearClient(gql=_pager(pages))
    out = c.list_customers()
    assert [x.name for x in out] == ["A", "B"]


def test_status_and_tier_ids_from_dedicated_workspace_queries():
    # must come from customerStatuses/customerTiers (complete), not from customers
    statuses = {
        "data": {
            "customerStatuses": {
                "nodes": [
                    {"id": "act", "name": "Active"},
                    {"id": "poc", "name": "POC"},
                    {"id": "lost", "name": "Lost"},
                ]
            }
        }
    }
    tiers = {"data": {"customerTiers": {"nodes": [{"id": "t1", "name": "Tier 1"}]}}}
    assert LinearClient(gql=_pager([statuses])).status_ids() == {
        "Active": "act",
        "POC": "poc",
        "Lost": "lost",
    }
    assert LinearClient(gql=_pager([tiers])).tier_ids() == {"Tier 1": "t1"}


def test_create_customer_parses_result():
    result = {"data": {"customerCreate": {"success": True, "customer": _node("New", id="new-id")}}}
    c = LinearClient(gql=_pager([result]))
    live = c.create_customer(
        name="New",
        domains=["n.com"],
        external_ids=["new"],
        status_id="act",
        revenue=1000,
    )
    assert live.id == "new-id" and live.name == "New"


def test_update_customer_maps_python_fields_to_linear_input():
    captured = {}

    def gql(query, variables=None):
        captured["vars"] = variables
        return {"data": {"customerUpdate": {"success": True, "customer": _node("X", id="x")}}}

    c = LinearClient(gql=gql)
    c.update_customer(
        "x",
        revenue=5,
        status_id="act",
        tier_id="t1",
        external_ids=["x"],
        domains=["x.com"],
        name="X",
    )
    inp = captured["vars"]["input"]
    assert inp["statusId"] == "act" and inp["tierId"] == "t1"
    assert inp["externalIds"] == ["x"] and inp["revenue"] == 5


def _recording_gql():
    calls = []

    def gql(query, variables=None):
        calls.append(variables)
        return {"data": {"customerUpdate": {"success": True, "customer": _node("X", id="x")}}}

    gql.calls = calls
    return gql


def test_update_refuses_null_without_retire():
    gql = _recording_gql()
    with pytest.raises(LinearError, match="refusing to send empty revenue"):
        LinearClient(gql=gql).update_customer("c1", revenue=None)
    assert gql.calls == []


def test_update_refuses_empty_list_without_retire():
    gql = _recording_gql()
    with pytest.raises(LinearError, match="refusing to send empty domains"):
        LinearClient(gql=gql).update_customer("c1", domains=[])
    assert gql.calls == []


def test_update_allows_null_with_retire():
    gql = _recording_gql()
    LinearClient(gql=gql).update_customer("c1", retire=True, revenue=None)
    assert len(gql.calls) == 1
    assert gql.calls[0]["input"] == {"revenue": None}


@pytest.mark.parametrize(
    ("field", "empty"),
    [("name", ""), ("name", None), ("status_id", None), ("external_ids", [])],
)
def test_client_refuses_empty_name_even_with_retire(field, empty):
    gql = _recording_gql()
    with pytest.raises(LinearError, match=f"refusing to send empty {field}: Linear requires it"):
        LinearClient(gql=gql).update_customer("c1", retire=True, **{field: empty})
    with pytest.raises(LinearError, match=f"refusing to send empty {field}"):
        LinearClient(gql=gql).create_customer(**{field: empty})
    assert gql.calls == []


def test_create_refuses_null():
    gql = _recording_gql()
    with pytest.raises(LinearError, match="refusing to send empty tier_id"):
        LinearClient(gql=gql).create_customer(name="x", tier_id=None)
    assert gql.calls == []


def test_create_rejects_retire():
    gql = _recording_gql()
    with pytest.raises(TypeError, match="retire"):
        LinearClient(gql=gql).create_customer(name="x", retire=True)
    assert gql.calls == []


def test_delete_customer():
    c = LinearClient(gql=_pager([{"data": {"customerDelete": {"success": True}}}]))
    assert c.delete_customer("x") is True


def test_http_gql_retries_429_with_retry_after(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many", {"Retry-After": "0.01"}, io.BytesIO(b"")
            )
        return io.BytesIO(json.dumps({"data": {"ok": True}}).encode())

    monkeypatch.setattr("customer_registry_sync.linear_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "customer_registry_sync.linear_client.time.sleep", lambda s: sleeps.append(s)
    )
    out = LinearClient(token="t")._http_gql("{ x }")
    assert out == {"data": {"ok": True}} and calls["n"] == 2 and sleeps == [0.01]


def _raising(exc):
    def gql(query, variables=None):
        raise exc

    return gql


def test_get_null_node_is_none():
    assert LinearClient(gql=_pager([{"data": {"customer": None}}])).get_customer("x") is None


def test_get_not_found_error_is_none():
    assert LinearClient(gql=_raising(LinearNotFound("gone"))).get_customer("x") is None


def test_get_other_error_raises():
    with pytest.raises(LinearError, match="boom"):
        LinearClient(gql=_raising(LinearError("boom"))).get_customer("x")


def test_get_archived_node_is_none():
    node = _node("A", archivedAt="2026-10-30T00:00:00.000Z")
    assert LinearClient(gql=_pager([{"data": {"customer": node}}])).get_customer("a") is None


def test_get_unarchived_node_is_parsed():
    node = _node("A", archivedAt=None)
    got = LinearClient(gql=_pager([{"data": {"customer": node}}])).get_customer("a")
    assert got is not None and got.id == "a"


def test_customer_fields_read_archived_at():
    gql = _pager([{"data": {"customer": _node("A")}}])
    seen = []

    def recording(query, variables=None):
        seen.append(query)
        return gql(query, variables)

    LinearClient(gql=recording).get_customer("a")
    assert "archivedAt" in seen[0]


def _errors_response(monkeypatch, errors):
    monkeypatch.setattr(
        "customer_registry_sync.linear_client.urllib.request.urlopen",
        lambda req, timeout: io.BytesIO(json.dumps({"data": None, "errors": errors}).encode()),
    )


def test_http_gql_raises_not_found_when_the_errors_match(monkeypatch):
    _errors_response(monkeypatch, [{"message": "m"}])
    monkeypatch.setattr("customer_registry_sync.linear_client._is_not_found", lambda e: True)
    with pytest.raises(LinearNotFound):
        LinearClient(token="t")._http_gql("{ x }")


def test_http_gql_other_errors_are_not_not_found(monkeypatch):
    _errors_response(monkeypatch, [{"message": "m"}])
    monkeypatch.setattr("customer_registry_sync.linear_client._is_not_found", lambda e: False)
    with pytest.raises(LinearError) as info:
        LinearClient(token="t")._http_gql("{ x }")
    assert not isinstance(info.value, LinearNotFound)


def test_is_not_found_matches_no_error_shape_until_one_is_observed():
    """Linear's docs do not state the error for an unknown `customer(id:)`, so no
    error shape counts as not found yet (docs/linear-api-notes.md)."""
    from customer_registry_sync.linear_client import _is_not_found

    assert _is_not_found([]) is False
    assert _is_not_found([{"message": "anything", "extensions": {"type": "x"}}]) is False
