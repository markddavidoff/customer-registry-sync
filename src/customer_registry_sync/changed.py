"""Did a push touch a deploy input? Backs ``customer-registry-sync changed --since``.

The reusable workflow runs this first on a push and ends the run early when nothing
relevant changed. It replaces a hard-coded ``on.push.paths`` list, which GitHub cannot
read from the config file.

Deploy inputs are the generated desired rows (``<data_dir>/linear/``), the curated
files ``generate``, ``check`` and ``deploy`` read (``linear_files.CURATED_INPUTS``), and the
config file itself (it moves ``data_dir`` and names the workflow's environments). The state
mirror and the snapshots are not inputs: the state PR that a deploy opens touches only
those, and must not trigger another deploy.

Any doubt reports "changed": an unknown or unreachable sha (first push, force-push,
shallow clone), a git failure, or a ``data_dir`` outside the repository. A spurious run
waits at the approval gate; a skipped one would leave Linear behind silently. The git
comparison is ``gitops_engine.changed.changed_since``; this module only names the globs.
"""

from __future__ import annotations

from pathlib import Path

from gitops_engine.changed import git_toplevel, repo_relative

from .config import RegistryConfig
from .linear_files import CURATED_INPUTS
from .writers import DESIRED_SUBDIR


def _globs(cfg: RegistryConfig, top: Path) -> list[str] | None:
    """The deploy-input globs, or None when ``data_dir`` is outside the repo."""
    prefix = repo_relative(cfg.data_dir, top)
    if prefix is None:
        return None
    base = "" if prefix in ("", ".") else f"{prefix}/"
    globs = [f"{base}{DESIRED_SUBDIR}/**", *(f"{base}{name}" for name in CURATED_INPUTS)]
    if cfg.config_path is not None:
        config_rel = repo_relative(cfg.config_path, top)
        if config_rel is not None:
            globs.append(config_rel)
    return globs


def deploy_inputs(cfg: RegistryConfig, repo: Path | None = None) -> list[str]:
    """The repo-relative globs that are deploy inputs (``<dir>/**`` or an exact file).

    ``repo`` is any path inside the repository (default: the cwd). Raises ``ValueError``
    when it is not a git repository or ``data_dir`` is outside it.
    """
    top = git_toplevel(Path(repo) if repo is not None else Path.cwd())
    globs = _globs(cfg, top)
    if globs is None:
        raise ValueError(f"data_dir {cfg.data_dir} is outside the repository {top}")
    return globs
