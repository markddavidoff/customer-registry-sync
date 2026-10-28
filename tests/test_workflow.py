"""The example caller workflow (`examples/caller-workflow.yml`).

The reusable workflow and its checks live in gitops-engine (`.github/workflows/sync.yml`).
Here: the example parses, calls that workflow at the release this tool is built against,
passes every secret by name, and installs this tool at the same tag.
"""

from pathlib import Path

import yaml

from customer_registry_sync.check import orphan_lines
from customer_registry_sync.linear_client import LiveCustomer

ROOT = Path(__file__).resolve().parents[1]
CALLER = ROOT / "examples" / "caller-workflow.yml"
MODES = ("deploy", "restore", "e2e", "refresh")
SECRETS = {"destination-token", "destination-readonly-token", "app-id", "app-private-key"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


def test_example_caller_calls_the_engine_workflow_at_v0_2_0():
    (job,) = _load(CALLER)["jobs"].values()
    assert job["uses"] == "markddavidoff/gitops-engine/.github/workflows/sync.yml@v0.2.0"
    assert set(job["secrets"]) == SECRETS, "every declared secret is passed by name"
    assert job["permissions"] == {"contents": "read"}
    with_ = job["with"]
    assert with_["command"] == "customer-registry-sync"
    assert with_["config"] == "registry-sync.yaml"
    assert with_["token-env"] == "LINEAR_API_TOKEN"
    # The tool is installed from the same release the workflow comes from.
    assert with_["tool"].startswith("customer-registry-sync @ git+https://")
    assert with_["tool"].endswith("@v0.2.0")
    assert with_["mode"] == "${{ inputs.mode }}"
    assert with_["snapshot"] == "${{ inputs.snapshot }}"
    assert with_["rows"] == "${{ inputs.rows }}"


def test_example_caller_has_no_paths_filter():
    wf = _load(CALLER)
    triggers = _triggers(wf)
    assert triggers["push"] == {"branches": ["main"]}
    assert "paths" not in str(triggers)
    assert triggers["schedule"] and triggers["schedule"][0]["cron"]
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert dispatch["mode"]["options"] == list(MODES)
    assert {"snapshot", "rows"} <= set(dispatch)
    # A shared workflow-level group would let the daily refresh cancel a waiting deploy.
    assert "concurrency" not in wf


def test_example_caller_has_no_org_specific_values():
    text = CALLER.read_text()
    assert "customers/" not in text, "the data directory comes from config"
    assert "linear-deploy" not in text and "linear-readonly" not in text
    assert "ssh://" not in text


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
