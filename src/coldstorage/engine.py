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
        with tempfile.TemporaryDirectory(prefix="cold-unpack-") as tmp:
            # Unpack under the export's own name, not a constant. WhatsApp's
            # iOS zips are all just "_chat.txt", so the containing folder IS
            # the conversation name — with a fixed "export" every chat you
            # ingested collided into one thread and all but the first was
            # silently dropped as a duplicate.
            unpacked = ensure_unpacked(source, Path(tmp) / (_safe_dirname(source.stem) or "export"))
            connector = self._resolve_connector(unpacked, connector_id)
            if connector is None:
                raise ValueError(
                    "could not recognize this export. Pass --connector to force one of: "
                    + ", ".join(c.id for c in connectors.all_connectors())
                )

            # Keeping a raw copy is a nice-to-have; failing to make one must not
            # cost the user the actual backup. A dangling symlink inside an
            # export used to abort here, before a single record was stored.
            snapshot = None
            snapshot_error: str | None = None
            if keep_snapshot:
                try:
                    snapshot = self._snapshot(source, unpacked, connector.id)
                except Exception as exc:
                    snapshot_error = f"raw copy skipped ({type(exc).__name__}: {exc})"

            added = 0
            batches = 0
            errors: list[str] = []
            status = "ok"

            # One unreadable file must not cost the user the rest of their
            # export. A generator that raises is finished, so the loop is
            # driven by hand: a failure inside `next()` ends parsing, but a
            # failure while STORING one batch only skips that batch.
            it = iter(connector.parse_export(unpacked))
            while True:
                try:
                    batch = next(it)
                except StopIteration:
                    break
                except Exception as exc:  # parsing stopped; keep what we have
                    errors.append(f"{type(exc).__name__}: {exc}")
                    break
                try:
                    added += self.archive.ingest_batch(batch)
                    batches += 1
                except Exception as exc:  # this batch only
                    errors.append(f"{type(exc).__name__}: {exc}")

            # A recognized export that parsed no records at all is not a success.
            # The overwhelming cause is Meta's HTML export when we can only read
            # JSON; a truncated/corrupt download is the other. Recording this as
            # "ok" is exactly how someone comes to trust a backup that is empty —
            # the failure this whole tool exists to prevent. (A re-ingest of data
            # already stored still *parses* records, so batches > 0; only a
            # genuinely empty parse is flagged here.)
            if batches == 0:
                from .preflight import empty_export_reason

                errors.insert(0, empty_export_reason(unpacked, connector.id))
            if errors:
                status = "error"
            if snapshot_error:
                errors.append(snapshot_error)  # reported, but not a failed import
            # Keep every distinct reason, but bound what we store.
            error = "; ".join(dict.fromkeys(errors))[:2000] or None

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

    def ingest_folder(self, folder: Path, *, keep_snapshot: bool = True) -> list[IngestResult]:
        """Scan a folder (e.g. ~/Downloads) and ingest every export it recognizes.

        This is the simplest possible trigger: drop your exports in one place and
        run this (manually or on a schedule). Unrecognized items are skipped
        silently; already-ingested data is deduped, so it is safe to re-run.
        """
        folder = Path(folder)
        if not folder.is_dir():
            raise NotADirectoryError(folder)

        # Scan children first: each subfolder or .zip may be a separate export.
        results: list[IngestResult] = []
        for child in sorted(folder.iterdir()):
            if not (child.is_dir() or child.suffix.lower() == ".zip"):
                continue
            try:
                results.append(self.ingest(child, keep_snapshot=keep_snapshot))
            except Exception:
                # A password-protected zip, a dangling symlink, an unreadable
                # file — one bad item in ~/Downloads must not stop the sweep
                # before it reaches the real exports.
                continue  # not a recognizable export — skip quietly
        if results:
            return results

        # No child was a recognizable export → maybe the folder *itself* is one.
        try:
            return [self.ingest(folder, keep_snapshot=keep_snapshot)]
        except ValueError:
            return []

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
            # Name the copy after the export, so two folders ingested on the
            # same day don't silently overwrite (or skip) one another.
            target = dest / (_safe_dirname(original.name) or "export")
            if not target.exists():
                # Real exports contain symlinks, some of them dangling; copy the
                # link itself rather than chasing it into an error.
                shutil.copytree(
                    unpacked, target, symlinks=True, ignore_dangling_symlinks=True
                )
        return dest


def _safe_dirname(name: str) -> str:
    """A filesystem-safe directory name derived from an export's own name."""
    cleaned = "".join(c for c in (name or "") if c.isalnum() or c in " ._-()&+").strip(" .")
    return cleaned[:120]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
