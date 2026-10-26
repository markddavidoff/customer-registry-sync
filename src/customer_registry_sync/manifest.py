"""Content-hash manifests for generated directories.

``ingest``, ``generate`` and ``sync-inbound`` each write ``.manifest.yaml`` next
to the files they produce: the generator's name, a timestamp, and a sha256 per
``*.yaml`` file. Two readers use it:

* ``guard_overwrite`` — a generator asks before replacing a directory whose files
  were edited by hand since it last wrote them.
* ``check.offline_errors`` — CI fails on hand-edited generated files.

A directory with no manifest has no recorded baseline, so it counts as "no edits"
to the guard; ``check`` reports the missing manifest separately.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

MANIFEST = ".manifest.yaml"

PROMPT = "These files were edited by hand and will be replaced. Continue? [y/N] "


def _hash_files(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.glob("*.yaml"))
        if p.name != MANIFEST
    }


def write_manifest(directory: Path, generator: str) -> None:
    """Record the directory's file hashes. When the generator and every hash match
    the existing manifest, write nothing: a rerun on unchanged inputs keeps the old
    ``written_at``, so the daily refresh opens no PR when nothing changed."""
    files = _hash_files(directory)
    path = directory / MANIFEST
    if path.is_file():
        old = yaml.safe_load(path.read_text()) or {}
        if (
            old.get("generator") == generator
            and old.get("files") == files
            and old.get("written_at")
        ):
            return
    body = {
        "generator": generator,
        "written_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }
    (directory / MANIFEST).write_text(
        yaml.safe_dump(body, sort_keys=False, default_flow_style=False, allow_unicode=True)
    )


def manifest_edits(directory: Path) -> list[str] | None:
    """Hand edits since the manifest was written, or ``None`` when there is no manifest."""
    path = directory / MANIFEST
    if not path.is_file():
        return None
    recorded = (yaml.safe_load(path.read_text()) or {}).get("files") or {}
    actual = _hash_files(directory)
    edits = [f"modified: {n}" for n in recorded if n in actual and actual[n] != recorded[n]]
    edits += [f"added: {n}" for n in actual if n not in recorded]
    edits += [f"missing: {n}" for n in recorded if n not in actual]
    return sorted(edits)


def guard_overwrite(directory: Path, generator: str, *, no_input: bool) -> bool:
    """True when the generator may overwrite ``directory``.

    No manifest or no edits: True. Hand edits: print them, then refuse when input
    is unavailable (``--no-input`` or no terminal) and otherwise ask.
    """
    edits = manifest_edits(directory)
    if not edits:
        return True
    print(f"{directory}: files edited by hand since `{generator}` last wrote them:")
    for edit in edits:
        print(f"  {edit}")
    if no_input or not sys.stdin.isatty():
        return False
    return input(PROMPT).strip().lower() == "y"
