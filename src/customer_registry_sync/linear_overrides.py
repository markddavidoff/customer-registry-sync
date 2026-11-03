"""`linear-overrides.yaml`: every curated decision that differs from upstream, keyed by
canonical id, bound on the engine's overrides format (`gitops_engine.overrides`).

This module names the file and its keys, pins a name without its ` [Status]` suffix
(`pin_customer`, the callback `generate` and `check` pass to the engine), compares
names without the suffix (`base_value`), and checks the pinned values' types, which
the engine's format leaves to the consumer.

`generate` imports this module, so it imports nothing from `generate`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from gitops_engine.overrides import OverrideEntry, Overrides, OverridesFormat, load_overrides

from .identity import strip_status_suffix
from .linear_schema import LinearCustomer

OVERRIDES_FILE = "linear-overrides.yaml"
OVERRIDES_HEADER = (
    "# Curated decisions that differ from upstream, by canonical id: pins, domain\n"
    "# adds and removes, pending Linear edits, hidden and deleted customers.\n"
)
OVERRIDES_FORMAT = OverridesFormat(
    OVERRIDES_FILE,
    OVERRIDES_HEADER,
    "customers",
    "linear_id",
    ("name", "status", "revenue", "tier"),
    ("domains",),
)


def pin_customer(row: LinearCustomer, pins: dict[str, Any]) -> LinearCustomer:
    """`row` with the pinned values. A pinned name is the base name: the status suffix,
    from the pinned status or else the row's, is still added."""
    status = pins.get("status", row.status)
    name = f"{pins.get('name', strip_status_suffix(row.name))} [{status}]"
    values = {k: v for k, v in pins.items() if k != "name"}
    return dataclasses.replace(row, **values, name=name)


def base_value(field: str, value: Any) -> Any:
    """The value an edit is compared and recorded as: a name without its status suffix."""
    return strip_status_suffix(value) if field == "name" and isinstance(value, str) else value


def load_overrides_file(data_dir: Path) -> Overrides:
    """`<data_dir>/linear-overrides.yaml`; a missing file has no entries. Raises
    `ValueError` on a file that is not the overrides shape."""
    return load_overrides(Path(data_dir) / OVERRIDES_FILE, OVERRIDES_FORMAT)


def uncurated(entry: OverrideEntry, current: OverrideEntry) -> OverrideEntry | None:
    """`entry` (from `adopt_entry`) without what `current` already decides: a field
    `current` pins, even to empty, and a domain it already adds or removes, compared
    case-insensitively. The store matches list values exactly, so a case variant would
    be written twice, and a curated remove stands. None when nothing is left."""
    curated = {
        d.lower() for d in [*current.add.get("domains", []), *current.remove.get("domains", [])]
    }
    pins = {k: v for k, v in entry.pin.items() if k not in current.pin}
    domains = [d for d in entry.add.get("domains", []) if d.lower() not in curated]
    adds = {"domains": domains} if domains else {}
    return dataclasses.replace(entry, pin=pins, add=adds) if pins or adds else None


def _type_failure(field: str, value: Any) -> str | None:
    if field == "revenue":
        ok = value is None or (isinstance(value, int) and not isinstance(value, bool))
        return None if ok else "revenue must be an integer or null"
    if field == "tier":
        return None if value is None or isinstance(value, str) else "tier must be a string or null"
    if field == "name":
        ok = isinstance(value, str) and bool(value.strip())
        return None if ok else "name must be a non-empty string"
    return None


def value_type_failures(ov: Overrides) -> list[str]:
    """A pin whose value Linear cannot hold: `revenue` an integer or null, `tier` a
    string or null, `name` a non-empty string. Each cites the entry's line."""
    out: list[str] = []
    for cid, entry in ov.entries.items():
        for field, value in entry.pin.items():
            why = _type_failure(field, value)
            if why is not None:
                out.append(f"{ov.file}:{entry.line}: {cid}: pin {why}")
    return out
