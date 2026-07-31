"""LinkedIn — Rail A ("Download your data" CSV export parser).

LinkedIn's export is a folder of CSV files. We parse the common ones
defensively by column name:

- ``messages.csv`` — UPPERCASE headers (CONVERSATION ID, FROM, TO, DATE,
  SUBJECT, CONTENT).
- ``Connections.csv`` — Title Case headers, and often 2-3 preamble "Notes:"
  lines *before* the real header row, which we skip.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, NormalizedRecord, RecordType
from .base import Connector, register

_MESSAGES_FILE = "messages.csv"
_CONNECTIONS_FILE = "Connections.csv"


def _norm_key(key: str | None) -> str:
    """Normalize a header name: strip, collapse case ('First Name' -> 'first name')."""
    return (key or "").strip().lower()


def _norm_row(row: dict) -> dict[str, str]:
    """Normalize a DictReader row to lowercase-stripped keys and stripped values."""
    out: dict[str, str] = {}
    for k, v in row.items():
        nk = _norm_key(k)
        if not nk:
            continue
        if isinstance(v, list):  # restkey overflow columns
            v = ",".join(x for x in v if x)
        out[nk] = (v or "").strip()
    return out


def _find_files(root: Path, name: str) -> list[Path]:
    """Find ``name`` (case-insensitively) at the root or anywhere below it."""
    lname = name.lower()
    hits = [f for f in [root / name, *root.glob(f"**/{name}")] if f.is_file()]
    for f in root.glob("**/*.csv"):
        if f.name.lower() == lname and f not in hits:
            hits.append(f)
    return hits


def _read_rows(path: Path, *, header_marker: str | None = None) -> tuple[list[str], list[dict]]:
    """Read a CSV into normalized-key dict rows.

    If ``header_marker`` is given, skip preamble lines (e.g. LinkedIn's
    "Notes:" blurb in Connections.csv) until a line containing it appears.
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()
    start = 0
    if header_marker is not None:
        marker = header_marker.lower()
        for i, line in enumerate(lines):
            if marker in line.lower():
                start = i
                break
        else:
            return [], []
    reader = csv.DictReader(lines[start:])
    rows = [_norm_row(r) for r in reader]
    header = [_norm_key(h) for h in (reader.fieldnames or [])]
    return header, rows


class LinkedInConnector(Connector):
    id = "linkedin"
    display_name = "LinkedIn"
    rail = "export"
    provides = ["messages", "connections"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_dir():
            return False
        for f in _find_files(path, _MESSAGES_FILE):
            try:
                header, _ = _read_rows(f)
            except Exception:
                continue
            if "content" in header or "conversation id" in header:
                return True
        return bool(_find_files(path, _CONNECTIONS_FILE))

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from self._parse_messages(root)
        yield from self._parse_connections(root)

    def _parse_messages(self, root: Path) -> Iterator[Batch]:
        for f in _find_files(root, _MESSAGES_FILE):
            try:
                _, rows = _read_rows(f)
            except Exception:
                continue
            records = []
            counters: dict[str, int] = {}
            for row in rows:
                conv = row.get("conversation id") or "unknown"
                i = counters.get(conv, 0)
                counters[conv] = i + 1
                text = row.get("content") or None
                subject = row.get("subject") or None
                if text is None and subject is None:
                    continue
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.MESSAGE,
                        uid=f"msg:{conv}:{i}",
                        created_at=row.get("date") or None,
                        text=text,
                        author=row.get("from") or None,
                        thread=conv,
                        extra={k: v for k, v in
                               (("subject", subject), ("to", row.get("to") or None)) if v},
                    )
                )
            if records:
                yield Batch(records=records)
            break  # only the first matching file

    def _parse_connections(self, root: Path) -> Iterator[Batch]:
        for f in _find_files(root, _CONNECTIONS_FILE):
            try:
                header, rows = _read_rows(f, header_marker="first name")
            except Exception:
                continue
            if "first name" not in header:
                continue
            records = []
            for row in rows:
                first = row.get("first name", "")
                last = row.get("last name", "")
                name = " ".join(p for p in (first, last) if p)
                url = row.get("url", "")
                if not name and not url:
                    continue
                key = url or "|".join((first, last, row.get("connected on", "")))
                digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                extra = {
                    k: v
                    for k, v in (
                        ("company", row.get("company") or None),
                        ("position", row.get("position") or None),
                        ("url", url or None),
                        ("email", row.get("email address") or None),
                    )
                    if v
                }
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.FOLLOWER,
                        uid=f"conn:{digest}",
                        created_at=row.get("connected on") or None,
                        text=name or None,
                        extra=extra,
                    )
                )
            if records:
                yield Batch(records=records)
            break  # only the first matching file


register(LinkedInConnector())
