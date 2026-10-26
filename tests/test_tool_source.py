"""`tool-source`: the e2e mode tests the version the caller's lock installed."""

import json

import pytest

from customer_registry_sync import cli
from customer_registry_sync.tool_source import ToolSource, ToolSourceError, tool_source

# Shapes as uv writes them (PEP 610), checked against this repo's own .venv.
GIT = {
    "url": "ssh://git@github.com/example-owner/customer-registry-sync.git",
    "vcs_info": {
        "vcs": "git",
        "commit_id": "0123456789abcdef0123456789abcdef01234567",
        "requested_revision": "v0.2.0",
    },
}
DIR = {"url": "file:///work/my%20repo/tool", "dir_info": {"editable": True}}


def test_git_install_gives_url_and_commit():
    src = tool_source(lambda: json.dumps(GIT))
    assert src == ToolSource(
        kind="git",
        location="ssh://git@github.com/example-owner/customer-registry-sync.git",
        commit="0123456789abcdef0123456789abcdef01234567",
    )
    assert src.line() == (
        "git 0123456789abcdef0123456789abcdef01234567 "
        "ssh://git@github.com/example-owner/customer-registry-sync.git"
    )


def test_directory_install_gives_the_path():
    src = tool_source(lambda: json.dumps(DIR))
    assert src == ToolSource(kind="dir", location="/work/my repo/tool")
    assert src.line() == "dir - /work/my repo/tool"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        json.dumps(
            {"url": "https://example.invalid/tool-0.2.0-py3-none-any.whl", "archive_info": {}}
        ),
        json.dumps(
            {"url": "https://example.invalid/repo", "vcs_info": {"vcs": "hg", "commit_id": "1"}}
        ),
        json.dumps({"url": "ssh://example.invalid/repo.git", "vcs_info": {"vcs": "git"}}),
    ],
)
def test_unrecoverable_source_is_refused(raw):
    with pytest.raises(ToolSourceError):
        tool_source(lambda: raw)


def test_cli_tool_source_reports_this_checkout(make_config, capsys):
    # The test venv installs this repo as an editable directory.
    assert cli.main(["--config", str(make_config()), "tool-source"]) == 0
    kind, commit, location = capsys.readouterr().out.strip().split(" ", 2)
    assert (kind, commit) == ("dir", "-")
    assert location


def test_cli_tool_source_exits_2_when_unrecoverable(make_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "tool_source", lambda: tool_source(lambda: None))
    assert cli.main(["--config", str(make_config()), "tool-source"]) == 2
    assert "direct_url.json" in capsys.readouterr().err
