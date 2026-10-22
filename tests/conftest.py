import os
import uuid
from datetime import UTC, datetime

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
            "hub_page_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "buckets": {
                name: f"bbbbbbbb-bbbb-4bbb-8bbb-{i:012d}" for i, name in enumerate(BUCKET_NAMES)
            },
            "data_dir": str(data_dir if data_dir is not None else cfg_dir / "customers"),
        }
        path = cfg_dir / "registry-sync.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    return _make


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
