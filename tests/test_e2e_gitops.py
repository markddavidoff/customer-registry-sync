"""Live e2e: scenarios 1-4 against throwaway zzz-gitops-test-<run>-* records.
Runs ONLY with `uv run pytest --run-live` and LINEAR_API_TOKEN in the environment. Every
record is namespaced, uses a *.invalid domain, and is deleted in teardown."""

import pytest

from customer_registry_sync import cli
from customer_registry_sync.config import DEFAULT_CONFIG_NAME
from customer_registry_sync.linear_schema import LinearCustomer
from customer_registry_sync.writers import load_state, write_desired

pytestmark = pytest.mark.live

BUCKET_NAMES = (
    "Customers",
    "Churned Customers",
    "Archive",
    "POCs",
    "Prospects",
    "Partner & Supporting Accounts",
)


def _mk(client, prefix, slug, revenue=1):
    name = f"{prefix}{slug}"
    return client.create_customer(
        name=name,
        domains=[f"{name}.invalid"],
        external_ids=[name],
        status_id=client.status_ids()["Active"],
        revenue=revenue,
    )


def _desired(prefix, slug, revenue):
    name = f"{prefix}{slug}"
    return LinearCustomer(
        name=name,
        domains=[f"{name}.invalid"],
        revenue=revenue,
        status="Active",
        external_ids=[name],
    )


def _run(*argv, out_dir, prefix):
    # The CLI always loads a config; these runs only touch Linear, so the page ids are dummies.
    cfg = out_dir / DEFAULT_CONFIG_NAME
    cfg.write_text(
        "hub_page_id: unused\nbuckets:\n" + "".join(f"  {name}: unused\n" for name in BUCKET_NAMES)
    )
    return cli.main(["--config", str(cfg), *argv, "--out-dir", str(out_dir), "--scope", prefix])


def _live_revenue(client, cid):
    return client.get_customer(cid).revenue


def test_scenario1_happy_in_sync_change(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb1", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    write_desired([_desired(run_prefix, "rb1", revenue=5)], tmp_path)  # the PR's change
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # CI green
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _live_revenue(live_client, lv.id) == 5
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # state == live again


def test_scenario2_drift_at_check(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb2", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    write_desired([_desired(run_prefix, "rb2", revenue=5)], tmp_path)
    live_client.update_customer(lv.id, revenue=77)  # out-of-band edit
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 3  # CI red
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0  # resolution
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0


def test_scenario3_drift_at_deploy(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb3", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    write_desired([_desired(run_prefix, "rb3", revenue=5)], tmp_path)
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # merged green
    live_client.update_customer(lv.id, revenue=88)  # changes before deploy
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 3  # abort, no clobber
    assert _live_revenue(live_client, lv.id) == 88


def test_scenario4_happy_no_change(live_client, run_prefix, tmp_path):
    _mk(live_client, run_prefix, "rb4", revenue=3)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    [st] = [s for s in load_state(tmp_path) if s.name.endswith("rb4")]
    write_desired(
        [
            LinearCustomer(
                name=st.name,
                domains=st.domains,
                revenue=st.revenue,
                status=st.status,
                external_ids=st.external_ids,
            )
        ],
        tmp_path,
    )
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0  # no-op
