"""`cold check` — inspect an export and report coverage WITHOUT storing anything.

This is how you verify a parser actually matches your real export before trusting
it: point it at your download and it prints how many messages, followers, posts,
and media it recognized (and warns about likely problems, like an HTML export or a
section that came back empty). Nothing is written; nothing is encrypted; it's a
pure read-and-count dry run.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import connectors
from .connectors.base import Connector, ensure_unpacked


@dataclass
class CheckResult:
    connector: str | None
    counts: dict[str, int] = field(default_factory=dict)  # by record type
    threads: int = 0
    media_refs: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def check_export(source: Path, *, connector_id: str | None = None) -> CheckResult:
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix="cold-check-") as tmp:
        unpacked = ensure_unpacked(source, Path(tmp) / "export")
        connector: Connector | None = (
            connectors.get(connector_id) if connector_id else connectors.detect_connector(unpacked)
        )
        if connector is None:
            return CheckResult(connector=None, warnings=_no_match_warnings(unpacked))

        counts: Counter[str] = Counter()
        threads: set[str] = set()
        media_refs = 0
        warnings: list[str] = []
        try:
            for batch in connector.parse_export(unpacked):
                for r in batch.records:
                    counts[r.type.value] += 1
                    if r.type.value == "message" and r.thread:
                        threads.add(r.thread)
                media_refs += len(batch.media)
        except Exception as exc:  # a parse error is information, not a crash
            warnings.append(f"parser raised {type(exc).__name__}: {exc}")

        warnings += _coverage_warnings(connector, counts)
        # Detected the platform but parsed nothing. HTML and JSON both parse now,
        # so this means a partial export that omitted the data we back up.
        if sum(counts.values()) == 0:
            warnings.append(
                "Detected the platform but found no messages, posts, or connections — "
                'this looks like a partial export; request "All available information".'
            )
        return CheckResult(
            connector=connector.id,
            counts=dict(counts),
            threads=len(threads),
            media_refs=media_refs,
            warnings=warnings,
        )


def empty_export_reason(path: Path, connector_id: str) -> str:
    """A user-actionable reason a recognized export parsed to nothing.

    Used by the engine so an ingest that stored zero records is reported as a
    real failure — never a silent success. HTML and JSON both parse now, so an
    empty result means the export simply didn't include the data we back up —
    almost always a partial "some of your information" export.
    """
    return (
        f"This {connector_id} export didn't contain any messages, posts, or connections "
        'to back up. When you request your data, choose "All available information" '
        "(not a subset), then add it again."
    )


def unrecognized_reasons(path: Path) -> list[str]:
    """Why an export wasn't recognized, in words a user can act on.

    Shared by `cold check` and `cold ingest` so both explain it identically. Takes
    a .zip or a folder; a zip is inspected by name so nothing is unpacked.
    """
    path = Path(path)
    reasons = ["This export was not recognized by any connector."]
    html = False
    if path.is_dir():
        html = bool(list(path.glob("**/message_*.html")) or list(path.glob("**/*.html")))
    elif path.suffix.lower() == ".zip":
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                html = any(n.lower().endswith(".html") for n in zf.namelist()[:5000])
        except (zipfile.BadZipFile, OSError):
            reasons.append("The .zip could not be opened — it may be corrupt or still downloading.")
            return reasons
    if html:
        reasons.append(
            "If this is an Instagram or Facebook export it may be partial — the app reads "
            'full exports (HTML or JSON), so request "All available information" and add it again.'
        )
    else:
        reasons.append(
            "If you know the platform, pass --connector to force it "
            "(see `cold connectors` for the list)."
        )
    return reasons


def _no_match_warnings(path: Path) -> list[str]:
    return unrecognized_reasons(path)


def _coverage_warnings(connector: Connector, counts: Counter[str]) -> list[str]:
    """Loud, generic warnings when a section a platform should have is empty."""
    w: list[str] = []
    provides = set(connector.provides)
    if "messages" in provides and counts.get("message", 0) == 0:
        w.append("No messages/DMs were found — check the export included your messages.")
    if {"followers"} & provides and counts.get("follower", 0) == 0:
        w.append("No followers were found — check the export included your connections.")
    return w
