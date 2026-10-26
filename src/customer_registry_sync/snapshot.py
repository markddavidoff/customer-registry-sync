"""Pre-apply snapshots of live Linear rows, and `restore` from them.

`deploy --apply` writes every live row it is about to update to
`<data_dir>/linear-snapshots/<UTC timestamp>.yaml` before its first write, then
records each row's post-write `updatedAt` as `written_updated_at`. `restore`
writes the snapshot values back with check-and-set: a row whose current
`updatedAt` is not the one the apply left behind (or, for an unwritten row, the
one the snapshot saw) was edited since, and is skipped and reported.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .linear_client import LinearError, LiveCustomer

SNAPSHOT_SUBDIR = "linear-snapshots"


@dataclass
class SnapshotRow:
    linear_id: str
    name: str
    domains: list[str]
    revenue: int | None
    tier: str | None
    status: str
    external_ids: list[str]
    updated_at: str
    written_updated_at: str | None = None


@dataclass
class RestoreResult:
    restored: list[str]
    skipped: list[tuple[str, str]]
    # "<linear_id>: <error>" when a write failed; later rows were not attempted
    failure: str | None = None


def _row(lv: LiveCustomer) -> SnapshotRow:
    return SnapshotRow(
        linear_id=lv.id,
        name=lv.name,
        domains=list(lv.domains),
        revenue=lv.revenue,
        tier=lv.tier,
        status=lv.status,
        external_ids=list(lv.external_ids),
        updated_at=lv.updated_at,
    )


def _dump(path: Path, taken_at: str, rows: list[SnapshotRow]) -> None:
    body = {"taken_at": taken_at, "rows": [asdict(r) for r in rows]}
    path.write_text(yaml.safe_dump(body, sort_keys=False, default_flow_style=False))


def write_snapshot(
    rows: list[LiveCustomer], snapshot_dir: Path, *, now: datetime | None = None
) -> Path:
    """Write `rows` to `<snapshot_dir>/<YYYYMMDDTHHMMSSZ>.yaml` and return its path.

    Creates the directory. Never overwrites an earlier snapshot: a second run in
    the same second gets a `-1`, `-2`, … suffix. Raises `OSError` when the file
    cannot be written, which callers treat as "abort before any write"."""
    when = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stem = when.strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"{stem}.yaml"
    n = 0
    while path.exists():
        n += 1
        path = snapshot_dir / f"{stem}-{n}.yaml"
    _dump(path, when.strftime("%Y-%m-%dT%H:%M:%SZ"), [_row(lv) for lv in rows])
    return path


def _load_raw(path: Path) -> tuple[str, list[SnapshotRow]]:
    raw = yaml.safe_load(path.read_text()) or {}
    known = {f.name for f in fields(SnapshotRow)}
    rows = []
    for r in raw.get("rows") or []:
        unknown = set(r) - known
        if unknown:
            raise ValueError(f"{path}: unknown snapshot key(s): {sorted(unknown)}")
        rows.append(
            SnapshotRow(
                **{
                    **r,
                    "domains": list(r.get("domains") or []),
                    "external_ids": list(r.get("external_ids") or []),
                }
            )
        )
    return str(raw.get("taken_at", "")), rows


def load_snapshot(path: Path) -> list[SnapshotRow]:
    return _load_raw(path)[1]


def record_written(path: Path, linear_id: str, updated_at: str) -> None:
    """Set `written_updated_at` on the row for `linear_id` and rewrite the file."""
    taken_at, rows = _load_raw(path)
    for r in rows:
        if r.linear_id == linear_id:
            r.written_updated_at = updated_at
    _dump(path, taken_at, rows)


def restore(path: Path, client, *, rows: list[str] | None = None) -> RestoreResult:
    """Write snapshot values back to Linear, skipping rows edited since.

    `rows` filters by external id. Every managed field is sent; a field the
    snapshot holds empty is sent empty with `retire=True` (the client refuses
    empties otherwise). Status and tier names must resolve before any write.
    A Linear error on a row sets `failure` and stops: rows restored so far stay
    listed, later rows are not attempted."""
    snap = load_snapshot(path)
    if rows is not None:
        wanted = set(rows)
        snap = [r for r in snap if wanted & set(r.external_ids)]
    status_ids = client.status_ids()
    tier_ids = client.tier_ids()
    missing = sorted(
        {r.status for r in snap if r.status and r.status not in status_ids}
        | {r.tier for r in snap if r.tier and r.tier not in tier_ids}
    )
    if missing:
        raise ValueError(f"unresolvable Linear status/tier, nothing restored: {missing}")

    result = RestoreResult(restored=[], skipped=[])
    for r in snap:
        cur = client.get_customer(r.linear_id)
        if cur is None:
            result.skipped.append((r.linear_id, "gone"))
            continue
        expected = r.written_updated_at or r.updated_at
        if cur.updated_at != expected:
            result.skipped.append(
                (r.linear_id, f"edited since snapshot: updated_at {cur.updated_at} != {expected}")
            )
            continue
        send = {
            "name": r.name,
            "domains": list(r.domains),
            "external_ids": list(r.external_ids),
            "revenue": r.revenue,
            "tier_id": tier_ids[r.tier] if r.tier else None,
        }
        if r.status:
            send["status_id"] = status_ids[r.status]
        retire = any(v is None or v == [] for v in send.values())
        try:
            client.update_customer(r.linear_id, retire=retire, **send)
        except LinearError as e:
            result.failure = f"{r.linear_id}: {e}"
            break
        result.restored.append(r.linear_id)
    return result
