"""Checks of the reusable registry-sync workflow and its example caller.

No network and no Actions run: these parse the YAML and pin the gates the workflow
must keep (modes, approval environment, snapshot artifact, App-token PRs, separate
concurrency groups, explicit secrets, no org-specific names). Two scripts (the mode
decision and the SSH-key guard) are run locally under bash with stub commands.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from customer_registry_sync.check import orphan_lines
from customer_registry_sync.cli import build_parser
from customer_registry_sync.linear_client import LiveCustomer

ROOT = Path(__file__).resolve().parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "registry-sync.yml"
CALLER = ROOT / "examples" / "workflows" / "registry-sync.yml"
MODES = ("deploy", "restore", "e2e", "refresh")
SECRETS = {
    "LINEAR_API_TOKEN",
    "LINEAR_READONLY_TOKEN",
    "REGISTRY_SYNC_APP_ID",
    "REGISTRY_SYNC_APP_PRIVATE_KEY",
    "REGISTRY_SYNC_SSH_KEY",
}
# GitHub's published ed25519 host key for github.com.
GITHUB_ED25519 = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _step_using(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(action + "@")]


def _step_named(job: dict, prefix: str) -> dict:
    return next(s for s in job["steps"] if s.get("name", "").startswith(prefix))


def test_reusable_workflow_shape():
    wf = _load(REUSABLE)
    triggers = _triggers(wf)
    assert set(triggers) == {"workflow_call"}, "the reusable workflow has no triggers of its own"
    call = triggers["workflow_call"]
    inputs = call["inputs"]
    assert set(inputs) == {"mode", "snapshot", "rows", "config", "project_dir", "base_branch"}
    for mode in MODES:
        assert mode in inputs["mode"]["description"]
    assert set(call["secrets"]) == SECRETS
    assert all(spec["required"] is False for spec in call["secrets"].values())

    jobs = wf["jobs"]
    assert set(jobs) == {"plan", "deploy", "refresh"}
    plan = jobs["plan"]
    plan_runs = _runs(plan)
    # The first job decides whether to run, and reads the environments from config.
    assert "changed --since" in plan_runs
    assert "config-get workflow.deploy_environment" in plan_runs
    assert "config-get workflow.readonly_environment" in plan_runs
    assert set(plan["outputs"]) == {"run", "mode", "deploy_environment", "readonly_environment"}
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
    assert "crs check" in deploy_runs
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
        assert token[0]["with"]["app-id"] == "${{ secrets.REGISTRY_SYNC_APP_ID }}"
        assert token[0]["with"]["private-key"] == "${{ secrets.REGISTRY_SYNC_APP_PRIVATE_KEY }}"
        pr_step = next(s for s in job["steps"] if "gh pr create" in s.get("run", ""))
        assert pr_step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_e2e_tests_the_installed_tool_version():
    """No ref input and no default repository: e2e fetches the source the lock installed."""
    deploy = _load(REUSABLE)["jobs"]["deploy"]
    fetch = _step_named(deploy, "Fetch the installed tool's tests")
    assert "crs tool-source" in fetch["run"]
    assert 'checkout --quiet --detach "$commit"' in fetch["run"]
    e2e = _step_named(deploy, "e2e")
    assert e2e["working-directory"] == "${{ steps.tool.outputs.dir }}"
    assert not _step_using(deploy, "actions/checkout")[1:], "no second checkout of a fixed ref"


def test_deploy_and_refresh_use_separate_job_level_concurrency_groups():
    """A refresh must not cancel a deploy waiting for approval."""
    wf = _load(REUSABLE)
    # One shared group lets the daily refresh cancel a deploy that waits for approval.
    assert "concurrency" not in wf
    jobs = wf["jobs"]
    deploy, refresh = jobs["deploy"]["concurrency"], jobs["refresh"]["concurrency"]
    assert deploy["group"] != refresh["group"]
    # Each group carries the config input, so two registries in one repo do not share one.
    assert deploy["group"] == "registry-sync-deploy-${{ inputs.config }}"
    assert refresh["group"] == "registry-sync-refresh-${{ inputs.config }}"
    assert deploy["cancel-in-progress"] is False
    assert refresh["cancel-in-progress"] is False
    assert "concurrency" not in jobs["plan"]
    # The caller must not reintroduce a shared workflow-level group.
    assert "concurrency" not in _load(CALLER)


def _crs_invocations(job: dict) -> list[list[str]]:
    """Every `crs ...` argument list written in a job's run scripts.

    Handles one-line calls (also inside `$(...)`) and the `args=(...)` / `args+=(...)`
    array the apply step builds before `crs "${args[@]}"`. Values stay as written
    (`$SNAPSHOT`, `${BEFORE:-}`); only the shape is checked.
    """
    calls: list[list[str]] = []
    array: list[str] = []
    for line in _runs(job).splitlines():
        line = line.strip()
        if line.startswith(("#", "echo")):
            continue
        built = re.fullmatch(r"args\+?=\((.*)\)", line)
        if built:
            array += shlex.split(built.group(1))
            continue
        for found in re.finditer(r"\bcrs ([a-z][a-z-]*[^\n)|;&]*)", line):
            calls.append(shlex.split(found.group(1)))
    if array:
        calls.append(array)
    return calls


def test_workflow_invocations_match_the_cli_parser():
    """Every subcommand and flag the workflow passes to `crs` exists in the real parser."""
    parser, _ = build_parser()
    seen: set[str] = set()
    for name, job in _load(REUSABLE)["jobs"].items():
        for argv in _crs_invocations(job):
            try:
                parser.parse_args(argv)
            except SystemExit as e:  # argparse exits with 2 on an unknown command or flag
                raise AssertionError(
                    f"{name}: `crs {' '.join(argv)}` is not a valid CLI call"
                ) from e
            seen.add(argv[0])
    # Guards the scan itself: it must find the calls the workflow is known to make.
    assert seen == {
        "changed",
        "config-get",
        "check",
        "restore",
        "deploy",
        "tool-source",
        "sync-inbound",
        "ingest",
        "generate",
    }


def test_linear_tokens_reach_only_the_steps_that_use_them():
    wf = _load(REUSABLE)
    for name, job in wf["jobs"].items():
        assert "secrets" not in str(job.get("env", {})), f"{name} puts a secret at job level"
        for step in job["steps"]:
            env = step.get("env", {})
            if any("secrets.LINEAR" in str(v) for v in env.values()):
                assert set(env) == {"LINEAR_API_TOKEN"} or step.get("id") == "apply"
                assert re.search(
                    r"sync-inbound|deploy --apply|restore --from|check|run-live", step["run"]
                ), step.get("name")
    refresh_tokens = [
        s["env"]["LINEAR_API_TOKEN"]
        for s in wf["jobs"]["refresh"]["steps"]
        if "LINEAR_API_TOKEN" in s.get("env", {})
    ]
    assert refresh_tokens == ["${{ secrets.LINEAR_READONLY_TOKEN }}"]


def test_secrets_are_static_names():
    """Dynamic `secrets[...]` lookups depended on `inherit`; every secret is declared."""
    text = REUSABLE.read_text()
    assert "secrets[" not in text and "vars[" not in text
    used = set(re.findall(r"secrets\.([A-Z_]+)", text))
    assert used == SECRETS


def test_no_expressions_inside_run_scripts():
    """Inputs and outputs reach scripts through `env:`, so a crafted input cannot inject."""
    wf = _load(REUSABLE)
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def test_every_action_is_pinned_to_a_sha():
    for job in _load(REUSABLE)["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step


@pytest.mark.parametrize("path", [REUSABLE, CALLER])
def test_no_org_specific_values(path):
    text = path.read_text()
    assert "customers/" not in text, "the data directory comes from config"
    assert "linear-deploy" not in text and "linear-readonly" not in text
    assert "LINEAR_DEPLOY_APP" not in text


def test_reusable_workflow_names_no_owner():
    assert "markddavidoff" not in REUSABLE.read_text()


def test_github_host_key_is_pinned():
    text = REUSABLE.read_text()
    assert "ssh-keyscan" not in text
    assert GITHUB_ED25519 in text


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
    assert set(job["secrets"]) == SECRETS, "every declared secret is passed by name"
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


# --- the scripts themselves, run under bash with stubs -------------------------------


def _run_step(step: dict, tmp_path: Path, env: dict[str, str]) -> tuple[int, dict, str]:
    out = tmp_path / "github_output"
    out.write_text("")
    full_env = {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        **env,
    }
    proc = subprocess.run(
        ["bash", "-c", step["run"]], env=full_env, capture_output=True, text=True, check=False
    )
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc.returncode, outputs, proc.stderr


def _stub_crs(tmp_path: Path, changed_output: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "crs-calls"
    stub = bin_dir / "crs"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho {changed_output}\n')
    stub.chmod(0o755)
    return calls


@pytest.mark.parametrize(
    ("event", "mode_input", "changed", "want_mode", "want_run"),
    [
        ("schedule", "", "changed", "refresh", "true"),
        ("schedule", "deploy", "changed", "refresh", "true"),
        ("workflow_dispatch", "", "unchanged", "deploy", "true"),
        ("workflow_dispatch", "e2e", "unchanged", "e2e", "true"),
        ("workflow_dispatch", "restore", "unchanged", "restore", "true"),
        ("workflow_dispatch", "refresh", "unchanged", "refresh", "true"),
        ("push", "", "changed", "deploy", "true"),
        ("push", "", "unchanged", "deploy", "false"),
    ],
)
def test_plan_decides_mode_and_run(tmp_path, event, mode_input, changed, want_mode, want_run):
    decide = _step_named(_load(REUSABLE)["jobs"]["plan"], "Decide the mode")
    calls = _stub_crs(tmp_path, changed)
    code, outputs, _ = _run_step(
        decide, tmp_path, {"EVENT": event, "MODE_INPUT": mode_input, "BEFORE": "abc123"}
    )
    assert code == 0
    assert outputs == {"mode": want_mode, "run": want_run}
    # Only a push asks `changed`; dispatch and schedule always run their mode.
    asked = calls.exists() and "changed --since abc123" in calls.read_text()
    assert asked == (event == "push")


def test_plan_rejects_an_unknown_mode(tmp_path):
    decide = _step_named(_load(REUSABLE)["jobs"]["plan"], "Decide the mode")
    _stub_crs(tmp_path, "changed")
    code, outputs, err = _run_step(
        decide, tmp_path, {"EVENT": "workflow_dispatch", "MODE_INPUT": "nuke", "BEFORE": ""}
    )
    assert code == 2
    assert "unknown mode 'nuke'" in err
    assert outputs == {}


@pytest.mark.parametrize(
    ("lock_text", "want_code"),
    [
        ('source = { git = "ssh://git@example.invalid/owner/dep.git?tag=v1#abc" }\n', 2),
        ('source = { registry = "https://pypi.org/simple" }\n', 0),
        (None, 0),
    ],
)
def test_ssh_step_fails_loudly_without_a_key_for_ssh_dependencies(tmp_path, lock_text, want_code):
    ssh = _step_named(_load(REUSABLE)["jobs"]["plan"], "SSH key")
    project = tmp_path / "project"
    project.mkdir()
    if lock_text is not None:
        (project / "uv.lock").write_text(lock_text)
    (tmp_path / "bin").mkdir()
    code, _, err = _run_step(ssh, tmp_path, {"SSH_KEY": "", "PROJECT_DIR": str(project)})
    assert code == want_code
    if want_code:
        assert "REGISTRY_SYNC_SSH_KEY" in err
