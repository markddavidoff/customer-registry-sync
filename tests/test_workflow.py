"""Static checks of the reusable registry-sync workflow and its example caller.

No network and no Actions run: these parse the YAML and pin the gates the workflow
must keep (modes, approval environment, snapshot artifact, App-token PRs, separate
concurrency groups, no org-specific names).
"""

import re
from pathlib import Path

import pytest
import yaml

from customer_registry_sync.check import orphan_lines
from customer_registry_sync.linear_client import LiveCustomer

ROOT = Path(__file__).resolve().parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "registry-sync.yml"
CALLER = ROOT / "examples" / "workflows" / "registry-sync.yml"
MODES = ("deploy", "restore", "e2e", "refresh")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _step_using(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(action + "@")]


def test_reusable_workflow_shape():
    wf = _load(REUSABLE)
    triggers = _triggers(wf)
    assert set(triggers) == {"workflow_call"}, "the reusable workflow has no triggers of its own"
    inputs = triggers["workflow_call"]["inputs"]
    for name in ("mode", "snapshot", "rows", "config", "project_dir"):
        assert name in inputs
    for mode in MODES:
        assert mode in inputs["mode"]["description"]

    jobs = wf["jobs"]
    assert set(jobs) == {"plan", "deploy", "refresh"}
    plan = jobs["plan"]
    plan_runs = _runs(plan)
    # The first job decides whether to run, and reads the workflow settings from config.
    assert "changed --since" in plan_runs
    for key in (
        "workflow.deploy_environment",
        "workflow.readonly_environment",
        "workflow.app_id_secret",
        "workflow.app_key_secret",
        "workflow.linear_token_secret",
        "workflow.readonly_token_secret",
    ):
        assert f"config-get {key}" in plan_runs
    for mode in MODES:
        assert mode in plan_runs, f"the plan job does not validate mode {mode}"
    assert _step_using(plan, "actions/checkout")[0]["with"]["fetch-depth"] == 0

    deploy, refresh = jobs["deploy"], jobs["refresh"]
    for job in (deploy, refresh):
        assert job["needs"] == "plan"
        assert "needs.plan.outputs.run == 'true'" in job["if"]
        assert job["permissions"] == {"contents": "read"}
    assert "needs.plan.outputs.mode != 'refresh'" in deploy["if"]
    assert "needs.plan.outputs.mode == 'refresh'" in refresh["if"]

    # Deploy jobs are bound to the configured environments (the approval gate lives there).
    assert deploy["environment"] == "${{ needs.plan.outputs.deploy_environment }}"
    assert refresh["environment"] == "${{ needs.plan.outputs.readonly_environment }}"

    deploy_runs = _runs(deploy)
    assert "customer-registry-sync check" in deploy_runs or "crs check" in deploy_runs
    assert "deploy --apply --prod --no-input" in deploy_runs
    assert "restore --from" in deploy_runs
    assert "test_e2e_gitops.py --run-live" in deploy_runs

    upload = _step_using(deploy, "actions/upload-artifact")
    assert len(upload) == 1
    assert "linear-snapshots" in upload[0]["with"]["path"]
    assert upload[0]["if"].startswith("always()")

    refresh_runs = _runs(refresh)
    assert "sync-inbound" in refresh_runs
    assert "ingest --only linear-found" in refresh_runs
    assert "generate" in refresh_runs
    assert not re.search(r"\bcheck\b", refresh_runs.replace("checkout", ""))

    for job in (deploy, refresh):
        token = _step_using(job, "actions/create-github-app-token")
        assert len(token) == 1
        assert "needs.plan.outputs.app_id_secret" in token[0]["with"]["app-id"]
        assert "needs.plan.outputs.app_key_secret" in token[0]["with"]["private-key"]
        assert "gh pr create" in _runs(job)
        pr_step = next(s for s in job["steps"] if "gh pr create" in s.get("run", ""))
        assert pr_step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_deploy_and_refresh_use_separate_job_level_concurrency_groups():
    """A refresh must not cancel a deploy waiting for approval."""
    wf = _load(REUSABLE)
    # One shared group lets the daily refresh cancel a deploy that waits for approval.
    assert "concurrency" not in wf
    jobs = wf["jobs"]
    deploy, refresh = jobs["deploy"]["concurrency"], jobs["refresh"]["concurrency"]
    assert deploy["group"] != refresh["group"]
    assert deploy["cancel-in-progress"] is False
    assert refresh["cancel-in-progress"] is False
    assert "concurrency" not in jobs["plan"]
    # The caller must not reintroduce a shared workflow-level group.
    assert "concurrency" not in _load(CALLER)


def test_linear_tokens_reach_only_the_steps_that_use_them():
    wf = _load(REUSABLE)
    for name, job in wf["jobs"].items():
        assert "secrets" not in str(job.get("env", {})), f"{name} puts a secret at job level"
        for step in job["steps"]:
            env = step.get("env", {})
            if any("token_secret" in str(v) for v in env.values()):
                assert "LINEAR_API_TOKEN" in env
                assert re.search(
                    r"sync-inbound|deploy --apply|restore --from|check|run-live", step["run"]
                ), step.get("name")


def test_no_expressions_inside_run_scripts():
    """Inputs and outputs reach scripts through `env:`, so a crafted input cannot inject."""
    wf = _load(REUSABLE)
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def test_every_action_is_pinned_to_a_sha():
    for path in (REUSABLE,):
        for job in _load(path)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step


@pytest.mark.parametrize("path", [REUSABLE, CALLER])
def test_no_org_specific_values(path):
    text = path.read_text()
    assert "customers/" not in text, "the data directory comes from config"
    assert "linear-deploy" not in text and "linear-readonly" not in text
    assert "LINEAR_DEPLOY_APP" not in text
    assert "vars.LINEAR" not in text and "secrets.LINEAR" not in text


def test_example_caller_has_no_paths_filter():
    wf = _load(CALLER)
    triggers = _triggers(wf)
    assert triggers["push"] == {"branches": ["main"]}
    assert "paths" not in str(triggers)
    assert triggers["schedule"] and triggers["schedule"][0]["cron"]
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert dispatch["mode"]["options"] == list(MODES)
    assert {"snapshot", "rows"} <= set(dispatch)

    (job,) = wf["jobs"].values()
    assert job["uses"].split("@")[0].endswith("/.github/workflows/registry-sync.yml")
    assert job["secrets"] == "inherit"
    assert job["with"]["mode"] == "${{ inputs.mode }}"
    assert job["permissions"] == {"contents": "read"}


def test_orphan_hint_names_the_workflow_and_mode():
    caller = _load(CALLER)
    name = caller["name"]
    assert "refresh" in _triggers(caller)["workflow_dispatch"]["inputs"]["mode"]["options"]
    lv = LiveCustomer(
        id="lin-1",
        name="Example Co",
        domains=[],
        revenue=None,
        tier=None,
        status="Active",
        external_ids=[],
        updated_at="2026-01-01T00:00:00.000Z",
    )
    hint = "\n".join(orphan_lines(lv))
    assert f"dispatch the {name} workflow with mode=refresh" in hint
