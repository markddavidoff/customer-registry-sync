"""Linear Customers GraphQL client (re-runnable; token-based).

POSTs to the GraphQL endpoint with the
``LINEAR_API_TOKEN`` in the Authorization header (no ``Bearer`` prefix),
retrying 429 (honoring Retry-After) and transient 5xx. The transport is
injectable (``gql=``) so unit tests run without network. Schema + ids
documented in docs/linear-api-notes.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

GQL_URL = "https://api.linear.app/graphql"

_CUSTOMER_FIELDS = (
    "id name domains revenue externalIds updatedAt status { id name } tier { id name }"
)


@dataclass
class LiveCustomer:
    id: str
    name: str
    domains: list[str]
    revenue: int | None
    tier: str | None
    status: str
    external_ids: list[str]
    updated_at: str


def parse_customer_node(node: dict[str, Any]) -> LiveCustomer:
    return LiveCustomer(
        id=node["id"],
        name=node["name"],
        domains=list(node.get("domains") or []),
        revenue=node.get("revenue"),
        tier=(node.get("tier") or {}).get("name"),
        status=(node.get("status") or {}).get("name", ""),
        external_ids=list(node.get("externalIds") or []),
        updated_at=node["updatedAt"],
    )


class LinearError(RuntimeError):
    pass


class LinearClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        gql: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None,
        url: str = GQL_URL,
    ) -> None:
        self._token = token or os.environ.get("LINEAR_API_TOKEN")
        self._url = url
        self._gql = gql or self._http_gql

    # ---- transport ----
    def _http_gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._token:
            raise LinearError("LINEAR_API_TOKEN not set")
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {"Content-Type": "application/json", "Authorization": self._token}
        last: Exception | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(self._url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    out = json.load(r)
                if "errors" in out:
                    raise LinearError(json.dumps(out["errors"])[:500])
                return out
            except urllib.error.HTTPError as e:  # retry 429 (honor Retry-After) and 5xx
                last = e
                if e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    time.sleep(float(ra) if ra else 0.5 * (2**attempt))
                    continue
                if e.code < 500:
                    raise LinearError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
            except urllib.error.URLError as e:
                last = e
            time.sleep(0.5 * (2**attempt))
        raise LinearError(f"GraphQL request failed after retries: {last}")

    def _data(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._gql(query, variables)["data"]

    # ---- reads ----
    def _all_nodes(self) -> list[dict[str, Any]]:
        q = (
            "query($after: String){ customers(first: 250, after: $after){ "
            "pageInfo { hasNextPage endCursor } nodes { %s } } }" % _CUSTOMER_FIELDS
        )
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self._data(q, {"after": cursor})["customers"]
            out.extend(page["nodes"])
            info = page["pageInfo"]
            if not info["hasNextPage"]:
                break
            cursor = info["endCursor"]
        return out

    def list_customers(self) -> list[LiveCustomer]:
        return [parse_customer_node(n) for n in self._all_nodes()]

    def status_ids(self) -> dict[str, str]:
        # authoritative workspace statuses (NOT derived from current customers,
        # which miss statuses no customer currently has)
        nodes = self._data("{ customerStatuses { nodes { id name } } }")["customerStatuses"][
            "nodes"
        ]
        return {n["name"]: n["id"] for n in nodes}

    def tier_ids(self) -> dict[str, str]:
        nodes = self._data("{ customerTiers { nodes { id name } } }")["customerTiers"]["nodes"]
        return {n["name"]: n["id"] for n in nodes}

    def get_customer(self, customer_id: str) -> LiveCustomer | None:
        q = "query($id: String!){ customer(id: $id){ %s } }" % _CUSTOMER_FIELDS
        try:
            node = self._data(q, {"id": customer_id})["customer"]
        except LinearError:
            return None
        return parse_customer_node(node) if node else None

    # ---- writes ----
    @staticmethod
    def _input(fields: dict[str, Any], *, retire: bool = False) -> dict[str, Any]:
        """Map python field names to Linear input keys.

        Refuses a null or empty-list value unless ``retire`` is set, so a sync
        can never erase a value in Linear by accident.
        """
        key = {"status_id": "statusId", "tier_id": "tierId", "external_ids": "externalIds"}
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if (v is None or v == []) and not retire:
                raise LinearError(f"refusing to send empty {k} without retire")
            out[key.get(k, k)] = v
        return out

    def create_customer(self, **fields: Any) -> LiveCustomer:
        if "retire" in fields:
            raise TypeError("create_customer() takes no retire: a create never clears a value")
        q = (
            "mutation($i: CustomerCreateInput!){ customerCreate(input: $i){ "
            "success customer { %s } } }" % _CUSTOMER_FIELDS
        )
        d = self._data(q, {"i": self._input(fields)})
        return parse_customer_node(d["customerCreate"]["customer"])

    def update_customer(
        self, customer_id: str, *, retire: bool = False, **fields: Any
    ) -> LiveCustomer:
        q = (
            "mutation($id: String!, $input: CustomerUpdateInput!){ "
            "customerUpdate(id: $id, input: $input){ success customer { %s } } }" % _CUSTOMER_FIELDS
        )
        d = self._data(q, {"id": customer_id, "input": self._input(fields, retire=retire)})
        return parse_customer_node(d["customerUpdate"]["customer"])

    def delete_customer(self, customer_id: str) -> bool:
        q = "mutation($id: String!){ customerDelete(id: $id){ success } }"
        return bool(self._data(q, {"id": customer_id})["customerDelete"]["success"])
