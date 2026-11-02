"""A synthetic data repo built by the real commands over the JSON fixtures.

`build_data_repo(dest)` writes `dest/registry-sync.yaml` and `dest/customers/`, then runs
the real CLI against them with no network and no token: `ingest --only notion` (a fake
Notion workspace built from `tests/fixtures`), `sync-inbound` (a fake Linear listing the
customers in `linear_customers_response.json`), `ingest --only linear-found` and
`generate`. `adopt-live`'s tests and the synthetic migration run both use it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

FIXTURES = Path(__file__).parent / "fixtures"
BUCKET_NAMES = (
    "Customers",
    "Churned Customers",
    "Archive",
    "POCs",
    "Prospects",
    "Partner & Supporting Accounts",
)
# bucket -> [(page title, fixture file stem)]
NOTION_PAGES: dict[str, list[tuple[str, str]]] = {
    "Customers": [("Acme Robotics", "customer_acme_robotics")],
    "Churned Customers": [("Soylent", "churned_soylent_metrics")],
    "POCs": [("Globex Labs (US)", "poc_globex_labs")],
    "Prospects": [
        ("Initrode Systems", "prospect_initrode"),
        ("Methodology", "prospect_initrode"),
    ],
    "Partner & Supporting Accounts": [("Vandelay Cyber", "partner_vandelay_cyber")],
}
DATASETS_YAML = "datasets: [notion, linear-found]\n"
DATA_SUBDIR = "customers"
CONFIG_NAME = "registry-sync.yaml"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fake_notion_client() -> tuple[type, dict[str, str]]:
    """A `notion_client.Client` stand-in serving the fixture pages, and the bucket name
    -> page id map to put in the config's `source.buckets`."""
    tree: dict[str, list] = {}
    for bucket, entries in NOTION_PAGES.items():
        kids = []
        for i, (title, fixture) in enumerate(entries):
            page_id = f"{bucket}-page-{i}"
            kids.append({"type": "child_page", "id": page_id, "child_page": {"title": title}})
            tree[page_id] = _load(fixture)
        if bucket == "Partner & Supporting Accounts":
            kids.append({"type": "table", "id": "partner-table"})
            tree["partner-table"] = _load("partner_table_rows")
        tree[f"{bucket}-bucket"] = kids

    class _Children:
        def list(self, block_id, start_cursor=None, page_size=100):
            return {"results": tree.get(block_id, []), "has_more": False}

    class _Blocks:
        children = _Children()

    class FakeClient:
        def __init__(self, auth):
            self.auth = auth
            self.blocks = _Blocks()

    return FakeClient, {name: f"{name}-bucket" for name in BUCKET_NAMES}


def fixture_live() -> list:
    """The live customers in `linear_customers_response.json`, as `LinearClient` parses them."""
    from customer_registry_sync.linear_client import parse_customer_node

    nodes = _load("linear_customers_response")["data"]["customers"]["nodes"]
    return [parse_customer_node(n) for n in nodes]


class FakeLinear:
    """The reads `sync-inbound` makes, served from a fixed list of live customers."""

    def __init__(self, live: list) -> None:
        self.live = list(live)

    def list_customers(self) -> list:
        return list(self.live)

    def get_customer(self, customer_id: str):
        return next((lv for lv in self.live if lv.id == customer_id), None)


@contextmanager
def offline(live: list) -> Iterator[None]:
    """The fake Notion and fake Linear in place: a dummy Notion token, no Linear token."""
    from customer_registry_sync import cli

    client, _ = fake_notion_client()
    env = {"NOTION_API_TOKEN": "fake-token"}
    with (
        mock.patch("notion_client.Client", client),
        mock.patch.object(cli, "_linear_client", lambda: FakeLinear(live)),
        mock.patch.dict(os.environ, env),
    ):
        yield


def run(root: Path, *argv: str) -> int:
    """`customer-registry-sync --config <root>/registry-sync.yaml <argv>`."""
    from customer_registry_sync import cli

    return cli.main(["--config", str(root / CONFIG_NAME), *argv])


def build_data_repo(dest: Path) -> Path:
    """`dest` holding `registry-sync.yaml` and `customers/` (datasets, identities, the
    state mirror, `linear/` and the CSVs), built by the real commands. Returns `dest`."""
    dest = Path(dest)
    data = dest / DATA_SUBDIR
    data.mkdir(parents=True, exist_ok=True)
    (data / "datasets.yaml").write_text(DATASETS_YAML)
    _, buckets = fake_notion_client()
    (dest / CONFIG_NAME).write_text(
        yaml.safe_dump(
            {
                "source": {"kind": "notion", "hub_page_id": "hub", "buckets": buckets},
                "data_dir": DATA_SUBDIR,
            },
            sort_keys=False,
        )
    )
    steps = (
        ("ingest", "--only", "notion", "--no-input"),
        ("sync-inbound", "--no-input"),
        ("ingest", "--only", "linear-found", "--no-input"),
        ("generate", "--no-input"),
    )
    with offline(fixture_live()):
        for step in steps:
            code = run(dest, *step)
            if code != 0:
                raise RuntimeError(f"build_data_repo: {' '.join(step)} exited {code}")
    return dest
