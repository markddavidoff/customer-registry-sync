"""Read and append to `identities.yaml` (in `data_dir`), keeping hand-written comments.

`ingest` is the only writer. It appends new identities and missing values; it never
changes or removes a value a human wrote. The file is loaded with
ruamel.yaml's round-trip loader, so comments, key order and unchanged lines survive a
save. Appends go through `add_identity` / `add_values`; nothing is written until `save`,
and `save` writes only when something was appended.

Round-trip output uses one indent style (mappings 2, block sequences 4 with the dash
offset 2). A hand-edited file in another sequence style is re-indented on the first save
that appends something; a file with nothing to append is never rewritten.
"""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .identity import (
    SOURCE_KEYS,
    Identity,
    IdentityTable,
    load_text,
    norm_name,
    parse_identities,
)

IDENTITIES_HEADER = (
    "# One entry per company. The key is the canonical id, stamped into Linear as the\n"
    "# external id; it never changes. Merge duplicates by editing this file.\n"
)


def _yaml() -> YAML:
    yml = YAML(typ="rt")
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096  # never wrap a long name or domain list
    return yml


def _flow_list(values: list[str]) -> CommentedSeq:
    seq = CommentedSeq(values)
    seq.fa.set_flow_style()
    return seq


class IdentityStore:
    """The identity file, loaded for appending. Reads see pending appends via `.table`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.reports: list[str] = []
        self._dirty = False
        self._prefix = ""  # comment-only file content, kept ahead of the first entry
        self._data: CommentedMap
        if path.exists():
            text = path.read_text()
            loaded = load_text(text, path.name, round_trip=True)
            self.table = parse_identities(loaded, path.name)
            if loaded is None:
                self._data = CommentedMap()
                self._prefix = text if text.strip() else IDENTITIES_HEADER
            else:
                self._data = loaded
        else:
            self.table = IdentityTable()
            self._data = CommentedMap()
            self._prefix = IDENTITIES_HEADER

    # --- reads -----------------------------------------------------------------

    def _holder(self, kind: str, value: str, skip: str) -> str | None:
        """The other identity that already lists this domain or name, if any."""
        for cid, ident in self.table.entries.items():
            if cid == skip:
                continue
            if kind == "domain" and value.lower() in {d.lower() for d in ident.domains}:
                return cid
            if kind == "name" and norm_name(value) in {norm_name(n) for n in ident.names}:
                return cid
        return None

    # --- appends ---------------------------------------------------------------

    def add_identity(
        self, base_id: str, *, names: list[str], domains: list[str], sources: dict[str, str]
    ) -> str:
        """Append a new identity and return its id: `base_id`, else `base_id-2`, `-3`, ..."""
        if not base_id:
            raise ValueError("identities: canonical id must be a non-empty string")
        _check_source_keys(sources)
        cid, n = base_id, 1
        while cid in self.table.entries:
            n += 1
            cid = f"{base_id}-{n}"
        entry = CommentedMap()
        names, domains = _unique(names, norm_name), _unique(domains, str.lower)
        if names:
            entry["names"] = _flow_list(names)
        if domains:
            entry["domains"] = _flow_list(domains)
        if sources:
            entry["sources"] = CommentedMap(sources)
        if not entry:
            entry = _empty_map()
        self._data[cid] = entry
        self.table.entries[cid] = Identity(
            canonical_id=cid, names=list(names), domains=list(domains), sources=dict(sources)
        )
        self._dirty = True
        return cid

    def add_values(
        self,
        canonical_id: str,
        *,
        names: list[str] = (),  # type: ignore[assignment]
        domains: list[str] = (),  # type: ignore[assignment]
        sources: dict[str, str] | None = None,
    ) -> None:
        """Append the values the identity lacks. Never changes or removes one."""
        ident = self.table.entries.get(canonical_id)
        if ident is None:
            raise ValueError(f"identities: no identity {canonical_id!r}")
        sources = sources or {}
        _check_source_keys(sources)
        entry = self._data[canonical_id]
        if entry is None:
            entry = self._data[canonical_id] = _empty_map()
        self._append(entry, ident, "domains", "domain", domains, str.lower)
        self._append(entry, ident, "names", "name", names, norm_name)
        for key, value in sources.items():
            old = ident.sources.get(key)
            if old is None:
                if "sources" not in entry or entry["sources"] is None:
                    entry["sources"] = CommentedMap()
                entry["sources"][key] = value
                ident.sources[key] = value
                self._dirty = True
            elif old != value:
                self.reports.append(
                    f"identities: '{canonical_id}' keeps source {key}='{old}'; ingest saw '{value}'"
                )

    def _append(
        self,
        entry: CommentedMap,
        ident: Identity,
        key: str,
        kind: str,
        values: list[str],
        norm: Any,
    ) -> None:
        have: list[str] = getattr(ident, key)
        for value in values:
            if norm(value) == "" or norm(value) in {norm(h) for h in have}:
                continue
            holder = self._holder(kind, value, skip=ident.canonical_id)
            if holder is not None:
                self.reports.append(
                    f"identities: not adding {kind} '{value}' to '{ident.canonical_id}': "
                    f"already on '{holder}'"
                )
                continue
            if key not in entry or entry[key] is None:
                entry[key] = _flow_list([])
            entry[key].append(value)
            have.append(value)
            self._dirty = True

    # --- write -----------------------------------------------------------------

    def save(self) -> bool:
        """Write the file if anything was appended. Returns whether it wrote."""
        if not self._dirty:
            return False
        buf = StringIO()
        buf.write(self._prefix)
        if self._data:
            _yaml().dump(self._data, buf)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(buf.getvalue())
        os.replace(tmp, self.path)
        self._dirty = False
        return True


def _check_source_keys(sources: dict[str, str]) -> None:
    bad = sorted(set(sources) - set(SOURCE_KEYS))
    if bad:
        raise ValueError(f"identities: unknown source key(s): {bad}")
    for k, v in sources.items():
        if not isinstance(v, str) or not v:
            raise ValueError(f"identities: source {k} must be a non-empty string")


def _unique(values: list[str], norm: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        n = norm(v)
        if n and n not in seen:
            seen.add(n)
            out.append(v)
    return out


def _empty_map() -> CommentedMap:
    m = CommentedMap()
    m.fa.set_flow_style()
    return m
