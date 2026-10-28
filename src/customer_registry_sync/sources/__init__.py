"""Source adapters: the upstream systems the registry can be built from.

``registry-sync.yaml`` picks one by name: ``source: {kind: notion, ...}``. Adapters ship
inside this package; there are no entry points and nothing is loaded from the data repo.
"""

from __future__ import annotations

from .base import SourceAdapter, SourceConfigError, SourceError
from .notion import NotionSource

SOURCES: dict[str, type[SourceAdapter]] = {"notion": NotionSource}

__all__ = ["SOURCES", "NotionSource", "SourceAdapter", "SourceConfigError", "SourceError"]
