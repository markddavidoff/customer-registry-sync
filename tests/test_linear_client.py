import io
import json
import urllib.error
from pathlib import Path

from customer_registry_sync.linear_client import LinearClient, LiveCustomer, parse_customer_node

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
        tier_id=None,
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
