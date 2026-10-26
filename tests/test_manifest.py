import sys
from datetime import datetime

import yaml

from customer_registry_sync import manifest
from customer_registry_sync.manifest import (
    MANIFEST,
    guard_overwrite,
    manifest_edits,
    write_manifest,
)


def _dir_with_files(tmp_path):
    d = tmp_path / "linear"
    d.mkdir()
    (d / "a.yaml").write_text("name: A\n")
    (d / "b.yaml").write_text("name: B\n")
    return d


def _no_prompt(*_args):
    raise AssertionError("prompted")


def test_manifest_round_trip_clean(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    assert manifest_edits(d) == []
    body = yaml.safe_load((d / MANIFEST).read_text())
    assert body["generator"] == "generate"
    assert body["written_at"]
    assert sorted(body["files"]) == ["a.yaml", "b.yaml"]  # excludes the manifest itself
    assert all(len(h) == 64 for h in body["files"].values())


class _Clock:
    """A `datetime` stand-in for `manifest`: each `now()` is one hour later."""

    calls = 0

    @classmethod
    def now(cls, tz=None):
        cls.calls += 1
        return datetime(2026, 10, 1, cls.calls % 24, tzinfo=tz)


def _ticking_clock(monkeypatch):
    _Clock.calls = 0
    monkeypatch.setattr(manifest, "datetime", _Clock)


def test_write_manifest_is_byte_stable_when_files_unchanged(tmp_path, monkeypatch):
    _ticking_clock(monkeypatch)
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    first = (d / MANIFEST).read_bytes()
    mtime = (d / MANIFEST).stat().st_mtime_ns
    write_manifest(d, "generate")
    assert (d / MANIFEST).read_bytes() == first
    assert (d / MANIFEST).stat().st_mtime_ns == mtime  # nothing was written
    (d / "a.yaml").write_text("name: A2\n")
    write_manifest(d, "generate")
    body = yaml.safe_load((d / MANIFEST).read_text())
    assert body["written_at"] != yaml.safe_load(first)["written_at"]
    assert manifest_edits(d) == []


def test_detects_modified_added_missing(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("name: A edited\n")
    (d / "b.yaml").unlink()
    (d / "c.yaml").write_text("name: C\n")
    assert manifest_edits(d) == ["added: c.yaml", "missing: b.yaml", "modified: a.yaml"]


def test_guard_refuses_without_tty(tmp_path, monkeypatch, capsys):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert guard_overwrite(d, "generate", no_input=False) is False
    assert "modified: a.yaml" in capsys.readouterr().out


def test_guard_refuses_with_no_input_even_on_tty(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert guard_overwrite(d, "generate", no_input=True) is False


def test_guard_prompts_and_accepts_y(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    (d / "a.yaml").write_text("hand edit\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []

    def say_y(prompt=""):
        prompts.append(prompt)
        return " Y "

    monkeypatch.setattr("builtins.input", say_y)
    assert guard_overwrite(d, "generate", no_input=False) is True
    assert prompts == ["These files were edited by hand and will be replaced. Continue? [y/N] "]
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert guard_overwrite(d, "generate", no_input=False) is False
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert guard_overwrite(d, "generate", no_input=False) is False


def test_no_manifest_is_not_an_edit(tmp_path, monkeypatch):
    d = _dir_with_files(tmp_path)
    assert manifest_edits(d) is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert guard_overwrite(d, "generate", no_input=True) is True
    assert guard_overwrite(tmp_path / "does-not-exist", "generate", no_input=True) is True


def test_clean_dir_passes_guard(tmp_path):
    d = _dir_with_files(tmp_path)
    write_manifest(d, "generate")
    assert guard_overwrite(d, "generate", no_input=True) is True
