"""`syt check` — inspect an export and report coverage WITHOUT storing anything.

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
    with tempfile.TemporaryDirectory(prefix="syt-check-") as tmp:
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
        # Detected the platform but parsed nothing + HTML files present → almost
        # certainly an HTML export instead of JSON.
        if sum(counts.values()) == 0 and (
            list(unpacked.glob("**/message_*.html")) or list(unpacked.glob("**/*.html"))
        ):
            warnings.append(
                "Detected the platform but found no data, and there are .html files — "
                "you likely exported HTML; re-download choosing JSON."
            )
        return CheckResult(
            connector=connector.id,
            counts=dict(counts),
            threads=len(threads),
            media_refs=media_refs,
            warnings=warnings,
        )


def _no_match_warnings(path: Path) -> list[str]:
    warnings = ["This export was not recognized by any connector."]
    # Common cause: the user exported HTML instead of JSON.
    if list(path.glob("**/message_*.html")) or list(path.glob("**/*.html")):
        warnings.append(
            "It looks like an HTML export — re-download choosing JSON format, "
            "which is what this tool reads."
        )
    return warnings


def _coverage_warnings(connector: Connector, counts: Counter[str]) -> list[str]:
    """Loud, generic warnings when a section a platform should have is empty."""
    w: list[str] = []
    provides = set(connector.provides)
    if "messages" in provides and counts.get("message", 0) == 0:
        w.append("No messages/DMs were found — check the export included your messages.")
    if {"followers"} & provides and counts.get("follower", 0) == 0:
        w.append("No followers were found — check the export included your connections.")
    return w
