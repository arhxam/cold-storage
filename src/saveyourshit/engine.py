"""Orchestration: ingest an export into the encrypted, indexed archive.

This is the heart of the working Phase-1 path. Given a downloaded export (a folder
or ``.zip``), it:

1. detects which platform it belongs to (or uses an explicit connector),
2. copies the raw export into ``<connector>/snapshots/<date>/`` immutably,
3. parses it into normalized batches,
4. stores media as dedup'd blobs and records into the index + manifest,
5. writes a per-run report and records the run for the dead-man's-switch.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import connectors
from .connectors.base import Connector, ensure_unpacked
from .store.archive import Archive


@dataclass
class IngestResult:
    connector: str
    added: int
    batches: int
    snapshot: Path | None
    status: str = "ok"
    error: str | None = None


class Engine:
    def __init__(self, archive: Archive) -> None:
        self.archive = archive

    def ingest(
        self,
        source: Path,
        *,
        connector_id: str | None = None,
        keep_snapshot: bool = True,
    ) -> IngestResult:
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)

        started = _now()
        with tempfile.TemporaryDirectory(prefix="syt-unpack-") as tmp:
            unpacked = ensure_unpacked(source, Path(tmp) / "export")
            connector = self._resolve_connector(unpacked, connector_id)
            if connector is None:
                raise ValueError(
                    "could not recognize this export. Pass --connector to force one of: "
                    + ", ".join(c.id for c in connectors.all_connectors())
                )

            snapshot = self._snapshot(source, unpacked, connector.id) if keep_snapshot else None

            added = 0
            batches = 0
            error: str | None = None
            status = "ok"
            try:
                for batch in connector.parse_export(unpacked):
                    added += self.archive.ingest_batch(batch)
                    batches += 1
            except Exception as exc:  # keep partial progress, record the failure
                status = "error"
                error = f"{type(exc).__name__}: {exc}"

        finished = _now()
        self.archive.index.record_run(connector.id, started, finished, status, added, error)
        return IngestResult(
            connector=connector.id,
            added=added,
            batches=batches,
            snapshot=snapshot,
            status=status,
            error=error,
        )

    def _resolve_connector(self, path: Path, connector_id: str | None) -> Connector | None:
        if connector_id:
            return connectors.get(connector_id)
        return connectors.detect_connector(path)

    def _snapshot(self, original: Path, unpacked: Path, connector_id: str) -> Path:
        date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        dest = self.archive.layout.snapshots_dir(connector_id) / date
        dest.mkdir(parents=True, exist_ok=True)
        if original.is_file():
            shutil.copy2(original, dest / original.name)
        else:
            target = dest / "export"
            if not target.exists():
                shutil.copytree(unpacked, target)
        return dest


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
