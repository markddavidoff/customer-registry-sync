"""The identity table and the single `resolve` that decides "same customer".

`identities.yaml` (in `data_dir`) holds one entry per company, keyed by its permanent
canonical id (stamped into Linear as the external id). Every record, desired row
and live Linear customer is reduced to `ItemKeys`, and `resolve` matches those
keys against the table in a fixed order: source id, then domain, then name. The
first key that hits decides. Domains compare case-insensitively; names compare
through `norm_name`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from .linear_schema import LinearCustomer
from .schema import CustomerRecord

if TYPE_CHECKING:
    from .linear_client import LiveCustomer

IDENTITIES_FILE = "identities.yaml"

SOURCE_NOTION = "notion"
SOURCE_LINEAR = "linear"
SOURCE_KEYS = (SOURCE_NOTION, SOURCE_LINEAR)
_IDENTITY_FIELDS = ("names", "domains", "sources")

# Linear's customer search does not surface status, so every managed customer's
# display name carries it: "<name> [<status>]".
_STATUS_SUFFIX_RE = re.compile(r" \[(?:Active|Prospect|Churned)\]$")


def strip_status_suffix(name: str) -> str:
    """Remove a trailing ` [Active]` / ` [Prospect]` / ` [Churned]`, if present."""
    stripped = _STATUS_SUFFIX_RE.sub("", name)
    return stripped or name


def norm_name(name: str) -> str:
    """Status suffix stripped, then case, spaces and punctuation ignored."""
    return re.sub(r"[^a-z0-9]+", "", strip_status_suffix(name).lower())


@dataclass
class Identity:
    canonical_id: str
    names: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class IdentityTable:
    entries: dict[str, Identity] = field(default_factory=dict)


def _invalid(msg: str) -> ValueError:
    """Every malformed-file problem is a ValueError, whatever the cause."""
    return ValueError(msg)


def _str_list(cid: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"identity {cid!r}: {key} must be a list of strings")
    return list(value)


def _parse_identity(cid: str, data: Any) -> Identity:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _invalid(f"identity {cid!r} must be a mapping")
    unknown = sorted(set(data) - set(_IDENTITY_FIELDS))
    if unknown:
        raise ValueError(f"identity {cid!r}: unknown key(s): {unknown}")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        raise _invalid(f"identity {cid!r}: sources must be a mapping")
    bad = sorted(set(sources) - set(SOURCE_KEYS))
    if bad:
        raise ValueError(f"identity {cid!r}: unknown source key(s): {bad}")
    for k, v in sources.items():
        if not isinstance(v, str) or not v:
            raise ValueError(f"identity {cid!r}: source {k} must be a non-empty string")
    return Identity(
        canonical_id=cid,
        names=_str_list(cid, "names", data.get("names")),
        domains=_str_list(cid, "domains", data.get("domains")),
        sources=dict(sources),
    )


def parse_identities(raw: Any, name: str) -> IdentityTable:
    """Validate a parsed `identities.yaml` mapping into a table. `name` is for errors."""
    if raw is None:
        return IdentityTable()
    if not isinstance(raw, dict):
        raise _invalid(f"{name}: top level must be a mapping of canonical id -> identity")
    entries: dict[str, Identity] = {}
    for cid, data in raw.items():
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{name}: canonical id {cid!r} must be a non-empty string")
        entries[cid] = _parse_identity(cid, data)
    return IdentityTable(entries=entries)


def load_text(text: str, name: str, *, round_trip: bool = False) -> Any:
    """Parse YAML text with ruamel, which rejects a duplicate key (PyYAML keeps the last).

    A repeated canonical id would otherwise drop an identity from a hand-edited file
    without a word. Any parse failure is a `ValueError` naming the file."""
    yml = YAML(typ="rt" if round_trip else "safe", pure=True)
    if round_trip:
        yml.preserve_quotes = True  # keep hand-written quote styles through a save
    try:
        return yml.load(text)
    except DuplicateKeyError as e:
        m = re.search(r'found duplicate key "([^"]*)"', str(e))
        key = m.group(1) if m else "?"
        line = f" (line {e.problem_mark.line + 1})" if e.problem_mark else ""
        raise ValueError(
            f"{name}: duplicate key {key!r}{line}; each canonical id may appear once"
        ) from e
    except YAMLError as e:
        raise ValueError(f"{name}: not valid YAML: {e}") from e


def load_identities(path: Path) -> IdentityTable:
    """Load `identities.yaml`. A missing file is an empty table."""
    if not path.exists():
        return IdentityTable()
    return parse_identities(load_text(path.read_text(), path.name), path.name)


@dataclass(frozen=True)
class ItemKeys:
    """What one record / desired row / live customer offers to `resolve`."""

    label: str
    ids: tuple[str, ...] = ()
    notion: str | None = None
    linear_id: str | None = None
    domains: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


def keys_for_record(rec: CustomerRecord, *, dataset: str, source_id: str) -> ItemKeys:
    """Keys for a dataset record; `source_id` is its file stem."""
    label, domains, names = rec.canonical_name, tuple(rec.domains), (rec.canonical_name,)
    if dataset == "notion":
        return ItemKeys(label=label, notion=source_id, domains=domains, names=names)
    if dataset == "linear-found":
        return ItemKeys(
            label=label,
            ids=(source_id,),
            linear_id=rec.linear_customer_id,
            domains=domains,
            names=names,
        )
    raise ValueError(f"unknown dataset {dataset!r}")


def keys_for_live(lv: LiveCustomer) -> ItemKeys:
    return ItemKeys(
        label=lv.name,
        ids=tuple(lv.external_ids),
        linear_id=lv.id,
        domains=tuple(lv.domains),
        names=(lv.name,),
    )


def keys_for_row(row: LinearCustomer) -> ItemKeys:
    return ItemKeys(
        label=row.name,
        ids=tuple(row.external_ids),
        linear_id=row.linear_synced.linear_id if row.linear_synced else None,
        domains=tuple(row.domains),
        names=(row.name,),
    )


HitKey = Literal["source id", "domain", "name"]


@dataclass(frozen=True)
class Hit:
    canonical_id: str
    key: HitKey
    value: str


def _hits(item: ItemKeys, table: IdentityTable) -> list[Hit]:
    """Every (identity, key) match in resolution order, duplicates included."""
    out: list[Hit] = []
    for xid in item.ids:
        if xid in table.entries:
            out.append(Hit(xid, "source id", xid))
    for src, value in ((SOURCE_NOTION, item.notion), (SOURCE_LINEAR, item.linear_id)):
        if value:
            out.extend(
                Hit(i.canonical_id, "source id", value)
                for i in table.entries.values()
                if i.sources.get(src) == value
            )
    for d in item.domains:
        dl = d.lower()
        out.extend(
            Hit(i.canonical_id, "domain", d)
            for i in table.entries.values()
            if dl in {x.lower() for x in i.domains}
        )
    for n in item.names:
        nn = norm_name(n)
        if not nn:
            continue
        out.extend(
            Hit(i.canonical_id, "name", n)
            for i in table.entries.values()
            if nn in {norm_name(x) for x in i.names}
        )
    return out


def resolve(item: ItemKeys, table: IdentityTable) -> Hit | None:
    """The first hit in key order decides: source id, then domain, then name."""
    hits = _hits(item, table)
    return hits[0] if hits else None


def resolve_all(item: ItemKeys, table: IdentityTable) -> list[Hit]:
    """One hit per distinct identity the item reaches, each by its first key."""
    seen: set[str] = set()
    out: list[Hit] = []
    for h in _hits(item, table):
        if h.canonical_id not in seen:
            seen.add(h.canonical_id)
            out.append(h)
    return out


def conflict(item: ItemKeys, table: IdentityTable) -> str | None:
    """A message when the later keys pick a different identity than the first."""
    hits = resolve_all(item, table)
    if len(hits) < 2:
        return None
    a, b = hits[0], hits[1]
    return (
        f"{item.label} resolves to two identities: "
        f"{a.canonical_id} ({a.key} '{a.value}') and {b.canonical_id} ({b.key} '{b.value}')"
    )


def _dupes(what: str, pairs: list[tuple[str, str, str]]) -> list[str]:
    """`pairs` = (comparison key, shown value, canonical id), in table order."""
    owners: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for k, value, cid in pairs:
        shown.setdefault(k, value)
        ids = owners.setdefault(k, [])
        if cid not in ids:
            ids.append(cid)
    return [
        f"{what} '{shown[k]}' is on two identities: {', '.join(ids)}"
        for k, ids in owners.items()
        if len(ids) > 1
    ]


def table_errors(table: IdentityTable) -> list[str]:
    """A domain, name or source id listed on two identities."""
    ents = list(table.entries.values())
    errors = _dupes("domain", [(d.lower(), d, i.canonical_id) for i in ents for d in i.domains])
    errors += _dupes(
        "name", [(norm_name(n), n, i.canonical_id) for i in ents for n in i.names if norm_name(n)]
    )
    for src in SOURCE_KEYS:
        errors += _dupes(
            f"source {src}",
            [(i.sources[src], i.sources[src], i.canonical_id) for i in ents if src in i.sources],
        )
    return errors


def _implicit_id(row: LinearCustomer, taken: dict[str, Identity]) -> str:
    if row.external_ids:
        base = row.external_ids[0]
    else:
        base = re.sub(r"[^a-z0-9]+", "-", strip_status_suffix(row.name).lower()).strip("-")
        base = base or "customer"
    cid, n = base, 1
    while cid in taken:
        n += 1
        cid = f"{base}-{n}"
    return cid


def with_implicit(table: IdentityTable, rows: list[LinearCustomer]) -> IdentityTable:
    """A copy of `table` plus one identity per row that resolves to nothing.

    The implicit canonical id is the row's external id (generated rows carry
    `slug_for`), else a slug of its name. Rows are added in order, so a later
    row of the same company resolves to the identity an earlier one created."""
    out = IdentityTable(entries=dict(table.entries))
    for row in rows:
        if resolve(keys_for_row(row), out) is not None:
            continue
        cid = _implicit_id(row, out.entries)
        sources = {SOURCE_LINEAR: row.linear_synced.linear_id} if row.linear_synced else {}
        out.entries[cid] = Identity(
            canonical_id=cid,
            names=[strip_status_suffix(row.name)],
            domains=list(row.domains),
            sources=sources,
        )
    return out
