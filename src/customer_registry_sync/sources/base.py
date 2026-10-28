"""The source adapter interface and its errors.

A source adapter turns one upstream system into ``CustomerRecord`` rows. The data
repo's ``registry-sync.yaml`` picks one by name under ``source: {kind: <name>, ...}``;
adapters ship inside this package (see ``sources.SOURCES``), never loaded from the data
repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..schema import CustomerRecord


class SourceConfigError(Exception):
    """The ``source:`` section is invalid. The message names the offending key."""


class SourceError(Exception):
    """The source could not be read (missing credential, upstream failure)."""


class SourceAdapter(Protocol):
    """One upstream system the registry is built from."""

    name: str

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> SourceAdapter:
        """Build the adapter from its ``source:`` section; raise ``SourceConfigError``."""
        ...

    def ingest(self, data_dir: Path) -> list[CustomerRecord]:
        """Read the upstream system, merge the curated inputs in ``data_dir``, return records.

        Raise ``SourceError`` when the source cannot be read."""
        ...
