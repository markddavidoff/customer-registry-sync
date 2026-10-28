import os
import uuid
from datetime import UTC, datetime
from typing import ClassVar

import pytest
import yaml

BUCKET_NAMES = (
    "Customers",
    "Churned Customers",
    "Archive",
    "POCs",
    "Prospects",
    "Partner & Supporting Accounts",
)


@pytest.fixture
def make_config(tmp_path_factory):
    """Write a valid synthetic registry config and return its path.

    ``data_dir`` is the dataset directory the config points at (defaults to a
    fresh directory the test never uses).
    """

    def _make(data_dir=None):
        cfg_dir = tmp_path_factory.mktemp("cfg")
        data = {
            "source": {
                "kind": "notion",
                "hub_page_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "buckets": {
                    name: f"bbbbbbbb-bbbb-4bbb-8bbb-{i:012d}" for i, name in enumerate(BUCKET_NAMES)
                },
            },
            "data_dir": str(data_dir if data_dir is not None else cfg_dir / "customers"),
        }
        path = cfg_dir / "registry-sync.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    return _make


@pytest.fixture
def run_cli(make_config):
    """Call `cli.main` under a synthetic config: ``run_cli(argv, data_dir=None)``.

    ``data_dir`` is the configured data directory, the one `--prod` guards (the
    default is a fresh directory the test never uses, so `--out-dir` is not prod).
    One config is written per distinct ``data_dir``."""
    from customer_registry_sync import cli

    configs = {}

    def _run(argv, *, data_dir=None):
        key = str(data_dir)
        if key not in configs:
            configs[key] = make_config(data_dir=data_dir)
        return cli.main(["--config", str(configs[key]), *argv])

    return _run


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked live against the REAL Linear workspace",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live") and os.environ.get("LINEAR_API_TOKEN"):
        return
    skip = pytest.mark.skip(reason="live tests need --run-live and LINEAR_API_TOKEN")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_client():
    from customer_registry_sync.linear_client import LinearClient

    return LinearClient()


@pytest.fixture(scope="session")
def run_prefix():
    return f"zzz-gitops-test-{uuid.uuid4().hex[:8]}-"


BASE_PREFIX = "zzz-gitops-test-"


def _sweep(client, prefix: str) -> int:
    n = 0
    for c in client.list_customers():
        if c.name.startswith(prefix):
            client.delete_customer(c.id)
            n += 1
    return n


def _sweep_stale(client, max_age_s: int = 3600) -> int:
    """Setup sweep: delete leftovers from a CRASHED prior run (any zzz- record
    older than an hour) without touching a concurrent fresh run's records.

    Assumption pinned here: one live run finishes well inside max_age_s. Age is
    measured from each record's updated_at, so a concurrent run that is still
    mutating its records keeps them fresh, while a run stalled past the hour
    gets its own records swept by the next run's setup. Raise max_age_s before
    adding a live test that is slow enough to approach the window."""
    now = datetime.now(UTC)
    n = 0
    for c in client.list_customers():
        if not c.name.startswith(BASE_PREFIX):
            continue
        # Python >= 3.11 fromisoformat parses the trailing "Z" as UTC.
        age = (now - datetime.fromisoformat(c.updated_at)).total_seconds()
        if age > max_age_s:
            client.delete_customer(c.id)
            n += 1
    return n


@pytest.fixture(scope="session", autouse=True)
def _live_teardown(request, run_prefix):
    """Stale sweep at setup (crashed prior runs), then delete every record THIS
    run created in a finally. A SIGKILL mid-run still leaks until the next
    session's stale sweep (or a manual purge by BASE_PREFIX)."""
    if not (request.config.getoption("--run-live") and os.environ.get("LINEAR_API_TOKEN")):
        yield
        return
    from customer_registry_sync.linear_client import LinearClient

    client = LinearClient()
    _sweep_stale(client)
    try:
        yield
    finally:
        _sweep(client, run_prefix)


class RecordingLinear:
    """In-memory Linear for apply/restore tests. Rows are built with the real
    `parse_customer_node`; writes apply the sent fields to the stored row, honour
    the client's never-null rule, and log each call. When `snapshot_dir` is
    given, each write records which snapshot files existed at that moment."""

    STATUSES: ClassVar[dict[str, str]] = {"Active": "st_a", "Prospect": "st_p", "Churned": "st_c"}
    TIERS: ClassVar[dict[str, str]] = {"Tier 1": "t1", "Tier 2": "t2"}

    def __init__(self, rows, snapshot_dir=None):
        self.rows = {r.id: r for r in rows}
        self.snapshot_dir = snapshot_dir
        self.calls: list[tuple] = []
        self.tamper: dict[str, dict] = {}  # id -> fields Linear "changes" on write
        self.fail: dict[str, Exception] = {}  # id -> error raised by update_customer
        self._tick = 0

    @staticmethod
    def node(
        lid,
        name,
        *,
        domains=(),
        revenue=None,
        tier=None,
        status="Active",
        xids=(),
        updated="2026-10-01T00:00:00.000Z",
    ):
        from customer_registry_sync.linear_client import parse_customer_node

        return parse_customer_node(
            {
                "id": lid,
                "name": name,
                "domains": list(domains),
                "revenue": revenue,
                "externalIds": list(xids),
                "updatedAt": updated,
                "status": {"id": "s", "name": status} if status else None,
                "tier": {"id": "t", "name": tier} if tier else None,
            }
        )

    def _now(self):
        self._tick += 1
        return f"2026-11-01T00:00:{self._tick:02d}.000Z"

    def _snapshots(self):
        if self.snapshot_dir is None or not self.snapshot_dir.is_dir():
            return []
        return sorted(p.name for p in self.snapshot_dir.glob("*.yaml"))

    def updates(self):
        return [c for c in self.calls if c[0] == "update"]

    def list_customers(self):
        return list(self.rows.values())

    def status_ids(self):
        return dict(self.STATUSES)

    def tier_ids(self):
        return dict(self.TIERS)

    def get_customer(self, cid):
        self.calls.append(("get", cid))
        return self.rows.get(cid)

    def _apply(self, row, fields):
        from dataclasses import replace

        from customer_registry_sync.linear_client import LinearError

        names = {
            "status_id": {v: k for k, v in self.STATUSES.items()},
            "tier_id": {v: k for k, v in self.TIERS.items()},
        }
        attr = {"status_id": "status", "tier_id": "tier"}
        out = {}
        for k, v in fields.items():
            if k in names and v is not None:
                if v not in names[k]:
                    raise LinearError(f"unknown {k} {v}")
                v = names[k][v]
            out[attr.get(k, k)] = list(v) if isinstance(v, list) else v
        return replace(row, **out, updated_at=self._now())

    def update_customer(self, cid, *, retire=False, **fields):
        from dataclasses import replace

        from customer_registry_sync.linear_client import LinearError

        for k, v in fields.items():
            if (v is None or v == []) and not retire:
                raise LinearError(f"refusing to send empty {k} without retire")
        self.calls.append(("update", cid, dict(fields), retire, self._snapshots()))
        if cid in self.fail:
            raise self.fail[cid]
        new = self._apply(self.rows[cid], fields)
        if cid in self.tamper:
            new = replace(new, **self.tamper[cid])
        self.rows[cid] = new
        return new

    def create_customer(self, **fields):
        if "retire" in fields:
            raise TypeError("create_customer takes no retire")
        self.calls.append(("create", dict(fields), self._snapshots()))
        lid = f"lin_new{len(self.rows)}"
        new = self._apply(self.node(lid, fields["name"]), fields)
        self.rows[lid] = new
        return new


@pytest.fixture
def recording_linear():
    return RecordingLinear


@pytest.fixture
def fake_notion(monkeypatch):
    """A fake Notion workspace built from tests/fixtures: no network, no real token.

    Replaces ``notion_client.Client`` and sets a dummy ``NOTION_API_TOKEN``. Returns
    the bucket name -> page id map to put in the config's ``source.buckets``."""
    import json
    from pathlib import Path

    import notion_client

    fix = Path(__file__).parent / "fixtures"

    def load(name):
        return json.loads((fix / f"{name}.json").read_text())

    pages = {
        "Customers": [("Acme Robotics", "customer_acme_robotics")],
        "Churned Customers": [("Soylent", "churned_soylent_metrics")],
        "POCs": [("Globex Labs (US)", "poc_globex_labs")],
        "Prospects": [
            ("Initrode Systems", "prospect_initrode"),
            ("Methodology", "prospect_initrode"),
        ],
        "Partner & Supporting Accounts": [("Vandelay Cyber", "partner_vandelay_cyber")],
    }
    tree: dict[str, list] = {}
    for bucket, entries in pages.items():
        kids = []
        for i, (title, fixture) in enumerate(entries):
            page_id = f"{bucket}-page-{i}"
            kids.append({"type": "child_page", "id": page_id, "child_page": {"title": title}})
            tree[page_id] = load(fixture)
        if bucket == "Partner & Supporting Accounts":
            kids.append({"type": "table", "id": "partner-table"})
            tree["partner-table"] = load("partner_table_rows")
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

    monkeypatch.setattr(notion_client, "Client", FakeClient)
    monkeypatch.setenv("NOTION_API_TOKEN", "fake-token")
    return {name: f"{name}-bucket" for name in BUCKET_NAMES}
