"""`<data_dir>/linear-ignore.yaml`: Linear customers the sync never touches.

An entry matches a live customer by `linear_id` (stable across renames) or by
a name prefix (`startswith` on the raw Linear name). The `zzz-gitops-test-` prefix
is built in even when the file omits it; it applies to unscoped runs only, so a
`--scope zzz-gitops-test-…` run may write its own throwaway records.

`partition_live` is the one split of live customers into managed and ignored;
every consumer (`check`, `deploy`) takes the managed half. A missing file means
the built-in prefix only. Stale entries warn, never fail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .datasets import LINEAR_FOUND, DatasetRecord
from .generate import canonical_id_for
from .identity import (
    IdentityTable,
    keys_for_live,
    keys_for_record,
    keys_for_row,
    load_text,
    resolve,
    strip_status_suffix,
    with_implicit,
)
from .linear_client import LiveCustomer
from .linear_schema import LinearCustomer

IGNORE_FILE = "linear-ignore.yaml"
BUILTIN_PREFIX = "zzz-gitops-test-"
_BUILTIN_REASON = "live e2e harness throwaway records (built in; always present)"
_MIN_PREFIX = 4
IGNORE_HEADER = "# Linear customers the sync never touches. Each entry needs a reason.\n"


@dataclass(frozen=True)
class IgnoreEntry:
    linear_id: str
    name: str | None
    reason: str
    line: int


@dataclass(frozen=True)
class IgnorePrefix:
    prefix: str
    reason: str
    line: int | None  # None = built-in


def _where(match: IgnoreEntry | IgnorePrefix) -> str:
    return "built-in" if match.line is None else f"{IGNORE_FILE}:{match.line}"


@dataclass
class IgnoreList:
    customers: list[IgnoreEntry] = field(default_factory=list)
    prefixes: list[IgnorePrefix] = field(
        default_factory=lambda: [IgnorePrefix(BUILTIN_PREFIX, _BUILTIN_REASON, None)]
    )

    def active_prefixes(self, scope: str | None) -> list[IgnorePrefix]:
        """The prefixes in force for a run. Only the built-in prefix has a scoped
        exemption: a run whose `--scope` starts with it may write the harness's
        records. A file prefix applies under every scope."""
        exempt = bool(scope) and scope.startswith(BUILTIN_PREFIX)
        return [p for p in self.prefixes if not (exempt and p.line is None)]

    def match(
        self,
        linear_id: str,
        name: str,
        *,
        scope: str | None,
    ) -> IgnoreEntry | IgnorePrefix | None:
        """The entry ignoring this customer: by `linear_id`, else by an active
        name prefix (`active_prefixes`)."""
        for entry in self.customers:
            if entry.linear_id == linear_id:
                return entry
        for p in self.active_prefixes(scope):
            if name.startswith(p.prefix):
                return p
        return None


def _load_raw(path: Path) -> Any:
    try:
        return YAML(typ="rt", pure=True).load(path.read_text())
    except YAMLError as e:
        raise ValueError(f"{IGNORE_FILE}: not valid YAML: {e}") from e


def _shape_error(msg: str) -> ValueError:
    # a malformed file is a ValueError like every other curated-file problem
    return ValueError(msg)


def _items(raw: Any, key: str) -> list[tuple[int, dict]]:
    """(1-based line, mapping) for each item of the `key` list."""
    seq = raw.get(key) if raw is not None else None
    if seq is None:
        return []
    if not isinstance(seq, list):
        raise _shape_error(f"{IGNORE_FILE}: '{key}' must be a list")
    out: list[tuple[int, dict]] = []
    for i, item in enumerate(seq):
        line = seq.lc.item(i)[0] + 1
        if not isinstance(item, dict):
            raise _shape_error(f"{IGNORE_FILE}:{line}: each '{key}' item must be a mapping")
        out.append((line, item))
    return out


def _parse(path: Path) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    if not path.exists():
        return [], []
    raw = _load_raw(path)
    if raw is not None and not isinstance(raw, dict):
        raise _shape_error(f"{IGNORE_FILE}: top level must be a mapping")
    return _items(raw, "customers"), _items(raw, "name_prefixes")


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    return "" if value is None else str(value).strip()


def load_ignore(path: Path) -> IgnoreList:
    """The ignore list; the built-in prefix is always present. Raises
    `ValueError` on a file that is not the expected shape (`ignore_errors`
    reports the softer problems: missing reason, short prefix, duplicate id)."""
    customers, prefixes = _parse(path)
    out = IgnoreList()
    for line, item in customers:
        lid = _text(item, "linear_id")
        if lid:
            name = _text(item, "name") or None
            out.customers.append(IgnoreEntry(lid, name, _text(item, "reason"), line))
    for line, item in prefixes:
        prefix = _text(item, "prefix")
        if prefix and prefix != BUILTIN_PREFIX:
            out.prefixes.append(IgnorePrefix(prefix, _text(item, "reason"), line))
    return out


def ignore_errors(path: Path) -> list[str]:
    """What `check` fails on in the ignore file; empty when clean."""
    try:
        customers, prefixes = _parse(path)
    except ValueError as e:
        return [str(e)]
    errors: list[str] = []
    seen: set[str] = set()
    for line, item in customers:
        where = f"{IGNORE_FILE}:{line}"
        lid = _text(item, "linear_id")
        if not lid:
            errors.append(f"{where}: missing linear_id")
        if not _text(item, "reason"):
            errors.append(f"{where}: missing reason")
        if lid and lid in seen:
            errors.append(f"{where}: linear_id {lid} listed twice")
        seen.add(lid)
    for line, item in prefixes:
        where = f"{IGNORE_FILE}:{line}"
        prefix = _text(item, "prefix")
        if not _text(item, "reason"):
            errors.append(f"{where}: missing reason")
        if len(prefix) < _MIN_PREFIX:
            errors.append(f"{where}: prefix '{prefix}' is shorter than {_MIN_PREFIX} characters")
    return errors


def partition_live(
    live: list[LiveCustomer],
    ignore: IgnoreList,
    *,
    scope: str | None,
) -> tuple[list[LiveCustomer], list[LiveCustomer]]:
    """The one split of live customers: (managed, ignored), order kept."""
    managed: list[LiveCustomer] = []
    ignored: list[LiveCustomer] = []
    for lv in live:
        (ignored if ignore.match(lv.id, lv.name, scope=scope) else managed).append(lv)
    return managed, ignored


def ignore_conflicts(
    ignored: list[LiveCustomer],
    ignore: IgnoreList,
    entries: list[DatasetRecord],
    desired: list[LinearCustomer],
    table: IdentityTable,
    *,
    scope: str | None,
) -> tuple[list[str], list[str]]:
    """(failures, reports): an ignored customer that resolves to an identity with
    a desired row is a failure — unless that row is the `linear-found` stub of
    the same Linear id, which is reported: the next refresh drops it."""
    full = with_implicit(table, desired)
    row_for: dict[str, LinearCustomer] = {}
    for d in desired:
        hit = resolve(keys_for_row(d), full)
        if hit is not None:
            row_for.setdefault(hit.canonical_id, d)
    stub_ids: dict[str, set[str]] = {}
    for e in entries:
        if e.dataset != LINEAR_FOUND or not e.record.linear_customer_id:
            continue
        hit = resolve(keys_for_record(e.record, dataset=e.dataset, source_id=e.source_id), full)
        cid = hit.canonical_id if hit is not None else canonical_id_for(e, table)
        stub_ids.setdefault(cid, set()).add(e.record.linear_customer_id)
    failures: list[str] = []
    reports: list[str] = []
    for lv in ignored:
        hit = resolve(keys_for_live(lv), full)
        if hit is None or hit.canonical_id not in row_for:
            continue
        d = row_for[hit.canonical_id]
        name = strip_status_suffix(d.name)
        if d.linear_owned and lv.id in stub_ids.get(hit.canonical_id, set()):
            reports.append(
                f"stub '{name}' is for ignored Linear customer '{lv.name}': "
                "the next refresh drops it"
            )
            continue
        match = ignore.match(lv.id, lv.name, scope=scope)
        where = _where(match) if match is not None else IGNORE_FILE
        failures.append(
            f"desired record '{name}' shares {hit.key} '{hit.value}' with ignored Linear "
            f"customer '{lv.name}' ({where}). Remove that entry or change {name}."
        )
    return failures, reports


def stale_ignore(ignore: IgnoreList, live_all: list[LiveCustomer]) -> list[str]:
    """One warning per `linear_id` entry no longer in Linear (warn, never fail)."""
    ids = {lv.id for lv in live_all}
    return [
        f"stale ignore entry: {IGNORE_FILE}:{e.line}: {e.linear_id} is not in Linear"
        for e in ignore.customers
        if e.linear_id not in ids
    ]


def ignored_lines(
    ignored: list[LiveCustomer],
    ignore: IgnoreList,
    *,
    scope: str | None,
) -> list[str]:
    """`check`'s visibility lines: each ignored customer, then each active prefix."""
    lines: list[str] = []
    for lv in ignored:
        match = ignore.match(lv.id, lv.name, scope=scope)
        reason = match.reason if match is not None else ""
        lines.append(f"IGNORED: '{lv.name}' ({lv.id}): {reason}")
    for p in ignore.active_prefixes(scope):
        if p.line is None:
            lines.append(f"IGNORED PREFIX: {p.prefix} (built-in)")
        else:
            lines.append(f"IGNORED PREFIX: {p.prefix} ({_where(p)}): {p.reason}")
    return lines


def _round_trip_yaml() -> YAML:
    """The identity file's round-trip style (gitops_engine.identity_store._yaml), so both curated files
    keep one indentation: mappings 2, block sequences 4 with the dash offset 2."""
    yml = YAML(typ="rt")
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096
    return yml


def append_ignore(path: Path, *, linear_id: str, name: str, reason: str) -> bool:
    """Append `linear_id` to the file's `customers` list, keeping hand-written comments
    and quote styles (ruamel round trip). Returns False, writing nothing, when the id is
    already listed. A missing file is created with the header and the built-in prefix
    entry. Raises `ValueError` on an empty id or reason, or a file that is not the
    expected shape."""
    linear_id, reason = linear_id.strip(), reason.strip()
    if not linear_id:
        raise ValueError("ignore: linear_id must not be empty")
    if not reason:
        raise ValueError("ignore: reason must not be empty (each entry needs a reason)")
    prefix_text = ""  # comment-only file content, kept ahead of the entries
    fresh = False
    if path.exists():
        text = path.read_text()
        data = load_text(text, IGNORE_FILE, round_trip=True)
        if data is None:  # empty or comment-only
            prefix_text = text if text.strip() else IGNORE_HEADER
            data, fresh = CommentedMap(), True
    else:
        prefix_text, data, fresh = IGNORE_HEADER, CommentedMap(), True
    if not isinstance(data, CommentedMap):
        raise _shape_error(f"{IGNORE_FILE}: top level must be a mapping")
    if fresh:
        data["customers"] = CommentedSeq()
        data["name_prefixes"] = CommentedSeq(
            [
                CommentedMap(
                    prefix=DoubleQuotedScalarString(BUILTIN_PREFIX),
                    reason=_BUILTIN_REASON,
                )
            ]
        )
    customers = data.get("customers")
    if customers is None:
        customers = data["customers"] = CommentedSeq()
    if not isinstance(customers, CommentedSeq):
        raise _shape_error(f"{IGNORE_FILE}: 'customers' must be a list")
    for item in customers:
        if isinstance(item, dict) and str(item.get("linear_id", "")).strip() == linear_id:
            return False
    entry = CommentedMap()
    entry["linear_id"] = linear_id
    entry["name"] = name
    entry["reason"] = reason
    customers.append(entry)
    buf = StringIO()
    buf.write(prefix_text)
    _round_trip_yaml().dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue())
    os.replace(tmp, path)
    return True
