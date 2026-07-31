"""Status + the dead-man's-switch. Pure logic, so it is easy to test.

The single most common real-world data-loss cause is a backup that silently
stopped running. So "when did each connector last succeed, and is that stale?"
is a first-class computation, not an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config
from .store.archive import Archive

STALE_AFTER = timedelta(days=7)


@dataclass
class ConnectorStatus:
    connector: str
    records: int
    last_run_at: str | None
    last_status: str | None  # ok | error | None (never run)
    stale: bool
    added_last_run: int


@dataclass
class ArchiveStatus:
    home: str
    encrypted: bool
    total_records: int
    blob_count: int
    total_bytes: int
    connectors: list[ConnectorStatus]

    @property
    def any_stale(self) -> bool:
        return any(c.stale for c in self.connectors)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def is_stale(last_run_at: str | None, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=UTC)
    dt = _parse_iso(last_run_at)
    if dt is None:
        return True  # never ran successfully → treat as stale
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt) > STALE_AFTER


def compute_status(
    archive: Archive, config: Config, *, now: datetime | None = None
) -> ArchiveStatus:
    counts = archive.index.counts_by_connector()
    # union of connectors that have data or are configured
    names = set(counts) | set(config.connectors)
    connectors: list[ConnectorStatus] = []
    for name in sorted(names):
        run = archive.index.last_run(name)
        last_ok = None
        if run and run["status"] == "ok":
            last_ok = run["finished_at"]
        connectors.append(
            ConnectorStatus(
                connector=name,
                records=counts.get(name, 0),
                last_run_at=run["finished_at"] if run else None,
                last_status=run["status"] if run else None,
                stale=is_stale(last_ok, now),
                added_last_run=run["added"] if run else 0,
            )
        )
    return ArchiveStatus(
        home=str(archive.layout.home),
        encrypted=config.encrypt,
        total_records=archive.index.count(),
        blob_count=archive.blobs.count(),
        total_bytes=archive.blobs.total_bytes(),
        connectors=connectors,
    )


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
