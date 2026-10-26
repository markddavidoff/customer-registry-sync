"""Did a push touch a deploy input? Backs ``customer-registry-sync changed --since``.

The reusable workflow runs this first on a push and ends the run early when nothing
relevant changed. It replaces a hard-coded ``on.push.paths`` list, which GitHub cannot
read from the config file.

Deploy inputs are the generated desired rows (``<data_dir>/linear/``) and the curated
files ``generate``, ``check`` and ``deploy`` read (``curated.CURATED_INPUTS``). The state
mirror and the snapshots are not inputs: the state PR that a deploy opens touches only
those, and must not trigger another deploy.

Any doubt reports "changed": an unknown or unreachable sha (first push, force-push,
shallow clone), a git failure, or a ``data_dir`` outside the repository. A spurious run
waits at the approval gate; a skipped one would leave Linear behind silently.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import RegistryConfig
from .curated import CURATED_INPUTS
from .writers import DESIRED_SUBDIR


class _Unknown(Exception):
    """git could not answer; the caller treats it as "changed"."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise _Unknown(str(e)) from None
    if proc.returncode != 0:
        raise _Unknown(proc.stderr.strip())
    return proc.stdout


def _toplevel(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()


def _data_prefix(cfg: RegistryConfig, top: Path) -> str | None:
    """``data_dir`` as a repo-relative POSIX path, or None when it is outside the repo."""
    data_dir = Path(os.path.realpath(cfg.data_dir))
    try:
        rel = data_dir.relative_to(top)
    except ValueError:
        return None
    return rel.as_posix()


def _globs(prefix: str) -> list[str]:
    base = "" if prefix in ("", ".") else f"{prefix}/"
    return [f"{base}{DESIRED_SUBDIR}/**", *(f"{base}{name}" for name in CURATED_INPUTS)]


def deploy_inputs(cfg: RegistryConfig, repo: Path | None = None) -> list[str]:
    """The repo-relative globs that are deploy inputs (``<dir>/**`` or an exact file).

    ``repo`` is any path inside the repository (default: the cwd). Raises ``ValueError``
    when ``data_dir`` is outside it.
    """
    try:
        top = _toplevel(Path(repo) if repo is not None else Path.cwd())
    except _Unknown as e:
        raise ValueError(f"not a git repository: {e}") from None
    prefix = _data_prefix(cfg, top)
    if prefix is None:
        raise ValueError(f"data_dir {cfg.data_dir} is outside the repository {top}")
    return _globs(prefix)


def _matches(path: str, globs: list[str]) -> bool:
    for pattern in globs:
        if pattern.endswith("/**"):
            if path.startswith(pattern[: -len("**")]):
                return True
        elif path == pattern:
            return True
    return False


def changed_since(repo: Path, sha: str, cfg: RegistryConfig) -> bool:
    """True when a deploy input differs between ``sha`` and HEAD, or when unsure."""
    try:
        top = _toplevel(repo)
        prefix = _data_prefix(cfg, top)
        if prefix is None:
            return True
        # `^{commit}` with --verify refuses anything that is not a commit in this clone,
        # including option-shaped input, before it reaches `git diff`.
        base = _git(
            top, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}"
        ).strip()
        names = _git(top, "diff", "--no-renames", "--name-only", "-z", base, "HEAD")
    except _Unknown:
        return True
    globs = _globs(prefix)
    return any(_matches(name, globs) for name in names.split("\0") if name)
