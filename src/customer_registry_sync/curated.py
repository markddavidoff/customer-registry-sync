"""Hand-curated input for the Linear sync: the retire file.

``linear-retire.yaml`` is keyed by the desired row's first external id (the
identity's canonical id)::

    <external_id>:
      domains: [a.com, b.io]
      revenue: 120000
      tier: Tier 1

Every key is optional. It names the live values a reviewed change may remove,
and is never generated over by ``generate`` or ``deploy``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

RETIRE_FILE = "linear-retire.yaml"

# The curated files `generate`, `check` and `deploy` read directly, as names relative
# to the registry's `data_dir`. A change to any of them changes what `deploy` may
# write, so each triggers the deploy workflow.
CURATED_INPUTS: tuple[str, ...] = (
    "identities.yaml",
    "linear-ignore.yaml",
    "datasets.yaml",
    RETIRE_FILE,
)


@dataclass
class CuratedFields:
    domains: list[str] = field(default_factory=list)
    revenue: int | None = None
    tier: str | None = None

    def empty(self) -> bool:
        return not self.domains and self.revenue is None and self.tier is None


def load_curated(path: Path) -> dict[str, CuratedFields]:
    """Read a curated YAML file. A missing or empty file yields ``{}``."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        str(ext_id): CuratedFields(
            domains=[str(d) for d in (body or {}).get("domains") or []],
            revenue=(body or {}).get("revenue"),
            tier=(body or {}).get("tier"),
        )
        for ext_id, body in raw.items()
    }


def write_curated(entries: dict[str, CuratedFields], path: Path, *, header: str) -> None:
    """Write entries with sorted keys, omitting empty fields and empty entries.

    ``header`` is written as a leading ``#`` comment block.
    """
    body: dict[str, dict] = {}
    for ext_id in sorted(entries):
        fields = entries[ext_id]
        if fields.empty():
            continue
        item: dict = {}
        if fields.domains:
            item["domains"] = list(fields.domains)
        if fields.revenue is not None:
            item["revenue"] = fields.revenue
        if fields.tier is not None:
            item["tier"] = fields.tier
        body[ext_id] = item
    comment = "".join(f"# {line}".rstrip() + "\n" for line in header.splitlines())
    text = yaml.safe_dump(body, sort_keys=False, default_flow_style=False) if body else ""
    path.write_text(comment + text)
