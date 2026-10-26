"""Where the installed customer-registry-sync came from (PEP 610 ``direct_url.json``).

The reusable workflow's `e2e` mode runs the tool's own live tests, which are not
packaged. It fetches them from exactly the source the caller's lock installed: the
git URL and commit, or the local directory. Any other install (an index, a wheel
file) has no recoverable source, so `e2e` refuses rather than test another version.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from urllib.parse import unquote, urlparse

DIST_NAME = "customer-registry-sync"


class ToolSourceError(Exception):
    """The installed tool's source cannot be recovered."""


@dataclass(frozen=True)
class ToolSource:
    kind: str  # "git" or "dir"
    location: str  # git URL, or a local directory
    commit: str | None = None  # git only

    def line(self) -> str:
        """``<kind> <commit or -> <location>``: the location last, so it may hold spaces."""
        return f"{self.kind} {self.commit or '-'} {self.location}"


def _read_direct_url() -> str | None:
    try:
        return distribution(DIST_NAME).read_text("direct_url.json")
    except PackageNotFoundError:
        raise ToolSourceError(f"{DIST_NAME} is not installed") from None


def tool_source(read: Callable[[], str | None] = _read_direct_url) -> ToolSource:
    raw = read()
    if not raw:
        raise ToolSourceError(
            f"{DIST_NAME} was not installed from git or a local directory "
            "(no direct_url.json); its tests cannot be fetched at the installed version"
        )
    try:
        info = json.loads(raw)
        url = str(info["url"])
    except (ValueError, KeyError, TypeError):
        raise ToolSourceError("direct_url.json is not valid") from None
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict):
        commit = vcs.get("commit_id")
        if vcs.get("vcs") != "git" or not commit:
            raise ToolSourceError(f"unsupported install source {url}")
        return ToolSource(kind="git", location=url.removeprefix("git+"), commit=str(commit))
    parsed = urlparse(url)
    if parsed.scheme == "file" and "dir_info" in info:
        return ToolSource(kind="dir", location=unquote(parsed.path))
    raise ToolSourceError(f"unsupported install source {url}")
