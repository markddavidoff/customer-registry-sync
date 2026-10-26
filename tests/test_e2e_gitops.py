"""Live e2e: Runbooks 1-6 against throwaway zzz-gitops-test-<run>-* records.
Runs ONLY with `uv run pytest --run-live` and LINEAR_API_TOKEN in the environment. Every
record is namespaced, uses a *.invalid domain, and is deleted in teardown."""

import pytest

from customer_registry_sync import cli
from customer_registry_sync.config import DEFAULT_CONFIG_NAME
from customer_registry_sync.datasets import NOTION, dataset_dir
from customer_registry_sync.generate import generate_linear
from customer_registry_sync.identity import IDENTITIES_FILE
from customer_registry_sync.identity_store import IdentityStore
from customer_registry_sync.manifest import write_manifest
from customer_registry_sync.schema import CustomerRecord
from customer_registry_sync.snapshot import SNAPSHOT_SUBDIR
from customer_registry_sync.writers import (
    DESIRED_SUBDIR,
    dataset_files,
    load_dataset,
    load_state,
    slug_for,
    write_dataset,
    write_desired,
)

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


def _write_desired(out_dir, prefix, slug, revenue, domains=None):
    """Write a consistent generated tree for one throwaway customer, as the real
    pipeline would leave it: `datasets/notion/` (ingest), then `linear/` produced by
    `generate_linear` from that dataset (generate), each with its manifest so
    `check` finds no hand edits and no stale generate output. The dataset
    domain's first label is the slug, so the desired external id equals the one
    `_mk` gives the live row. Desired names carry generate's ` [Active]`
    suffix; the `--scope` prefix still matches. `identities.yaml` holds the
    customer's identity (canonical id = that external id), written by the real
    `IdentityStore`, so `check` and `deploy` pair it by identity."""
    name = f"{prefix}{slug}"
    rec = CustomerRecord(
        canonical_name=name,
        domains=domains if domains is not None else [f"{name}.invalid"],
        classification="real",
        arr=revenue,
    )
    write_dataset([rec], out_dir)
    write_manifest(dataset_dir(out_dir, NOTION), "ingest")
    [(stem, _)] = dataset_files([rec])
    store = IdentityStore(out_dir / IDENTITIES_FILE)
    if slug_for(rec) not in store.table.entries:
        store.add_identity(
            slug_for(rec), names=[name], domains=rec.domains, sources={"notion": stem}
        )
        store.save()
    write_desired(generate_linear(load_dataset(out_dir)), out_dir)
    write_manifest(out_dir / DESIRED_SUBDIR, "generate")


def _cli(*argv, out_dir):
    # The CLI always loads a config; these runs only touch Linear, so the page ids are dummies.
    cfg = out_dir / DEFAULT_CONFIG_NAME
    cfg.write_text(
        "hub_page_id: unused\nbuckets:\n" + "".join(f"  {name}: unused\n" for name in BUCKET_NAMES)
    )
    return cli.main(["--config", str(cfg), *argv, "--out-dir", str(out_dir)])


def _run(*argv, out_dir, prefix):
    return _cli(*argv, "--scope", prefix, out_dir=out_dir)


def _live_revenue(client, cid):
    return client.get_customer(cid).revenue


def test_runbook1_happy_in_sync_change(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb1", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    _write_desired(tmp_path, run_prefix, "rb1", revenue=5)  # the PR's change
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # CI green
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _live_revenue(live_client, lv.id) == 5
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # state == live again


def test_runbook2_drift_at_check(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb2", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    _write_desired(tmp_path, run_prefix, "rb2", revenue=5)
    live_client.update_customer(lv.id, revenue=77)  # out-of-band edit
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 3  # CI red
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0  # resolution
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0


def test_runbook3_drift_at_deploy(live_client, run_prefix, tmp_path):
    lv = _mk(live_client, run_prefix, "rb3", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    _write_desired(tmp_path, run_prefix, "rb3", revenue=5)
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0  # merged green
    live_client.update_customer(lv.id, revenue=88)  # changes before deploy
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 3  # abort, no clobber
    assert _live_revenue(live_client, lv.id) == 88


def test_runbook4_happy_no_change(live_client, run_prefix, tmp_path):
    _mk(live_client, run_prefix, "rb4", revenue=3)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    [st] = [s for s in load_state(tmp_path) if s.name.endswith("rb4")]
    _write_desired(
        tmp_path, run_prefix, "rb4", revenue=st.revenue, domains=st.domains
    )  # same values as live; only generate's status suffix on the name is new
    assert _run("check", out_dir=tmp_path, prefix=run_prefix) == 0
    assert (
        _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0
    )  # no value changes (name gains the status suffix)


def _live_domains(client, cid):
    return sorted(d.lower() for d in client.get_customer(cid).domains)


def test_runbook5_linear_only_domain_survives_apply(live_client, run_prefix, tmp_path):
    """A domain that exists only in Linear is never dropped by an apply (union
    rule) while the planned revenue change still lands."""
    name = f"{run_prefix}rb5"
    registry_domain = f"{name}.invalid"
    linear_only = f"{name}-linear-only.invalid"
    lv = live_client.create_customer(
        name=name,
        domains=[registry_domain, linear_only],
        external_ids=[name],
        status_id=live_client.status_ids()["Active"],
        revenue=1,
    )
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    # the registry knows only one of the two domains, plus a revenue change
    _write_desired(tmp_path, run_prefix, "rb5", revenue=5, domains=[registry_domain])
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _live_domains(live_client, lv.id) == sorted([registry_domain, linear_only])
    assert _live_revenue(live_client, lv.id) == 5


def test_runbook6_restore_returns_row_exactly(live_client, run_prefix, tmp_path):
    """`restore --from <snapshot>` puts the row back to its pre-apply values."""
    lv = _mk(live_client, run_prefix, "rb6", revenue=1)
    assert _run("sync-inbound", out_dir=tmp_path, prefix=run_prefix) == 0
    _write_desired(tmp_path, run_prefix, "rb6", revenue=5)
    assert _run("deploy", "--apply", out_dir=tmp_path, prefix=run_prefix) == 0
    assert _live_revenue(live_client, lv.id) == 5
    [snapshot] = sorted(
        (tmp_path / SNAPSHOT_SUBDIR).glob("*.yaml")
    )  # printed as `snapshot: <path>`
    # restore takes no --scope; a tmp --out-dir is non-prod, so no --prod / --break-glass
    assert _cli("restore", "--from", str(snapshot), out_dir=tmp_path) == 0
    assert _live_revenue(live_client, lv.id) == 1
    assert live_client.get_customer(lv.id).name == f"{run_prefix}rb6"
    assert _live_domains(live_client, lv.id) == [f"{run_prefix}rb6.invalid"]
