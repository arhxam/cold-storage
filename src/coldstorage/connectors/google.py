"""Google Takeout — Rail A (official "Google Takeout" export parser).

Parses the common, stable parts of a Takeout archive:

* YouTube watch history  (``YouTube and YouTube Music/history/watch-history.json``)
* YouTube search history (``YouTube and YouTube Music/history/search-history.json``)
* YouTube subscriptions  (``**/subscriptions/subscriptions.csv``)
* Google Chat messages   (``Google Chat/Groups/**/messages.json``)

Legacy Hangouts (``Hangouts/Hangouts.json``) is deliberately SKIPPED: its format
is a deeply-nested protobuf dump with many event/segment variants and it was
frozen when Hangouts shut down — not worth the parsing surface for now.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..models import Batch, NormalizedRecord, RecordType
from .base import Connector, load_json, register, stable_uid

# Google Chat's ``created_date`` looks like
# "Tuesday, January 2, 2024 at 10:15:00 AM UTC".
_CHAT_DATE_FMT = "%A, %B %d, %Y at %I:%M:%S %p %Z"

_SUBSCRIPTION_COLUMNS = {"Channel Id", "Channel Url", "Channel Title"}


def _chat_iso(raw: str | None) -> str | None:
    """Best-effort ISO-8601 conversion of a Google Chat timestamp string."""
    if not raw or not isinstance(raw, str):
        return None
    # Newer exports use U+202F (narrow no-break space) before AM/PM.
    cleaned = raw.replace("\u202f", " ").replace("\u00a0", " ")
    try:
        dt = datetime.strptime(cleaned, _CHAT_DATE_FMT)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC).isoformat()


def _is_subscriptions_csv(path: Path) -> bool:
    """True if ``path`` has the Takeout subscriptions.csv header columns."""
    try:
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
            header = next(csv.reader(fh), None)
    except OSError:
        return False
    return header is not None and _SUBSCRIPTION_COLUMNS.issubset({c.strip() for c in header})


class GoogleConnector(Connector):
    id = "google"
    display_name = "Google / YouTube"
    rail = "export"
    provides = ["watch_history", "search_history", "subscriptions", "messages"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_dir():
            return False
        folder_markers = ["YouTube and YouTube Music", "Google Chat"]
        for marker in folder_markers:
            if (path / marker).is_dir() or any(path.glob(f"**/{marker}")):
                return True
        if (path / "archive_browser.html").exists() or any(path.glob("**/archive_browser.html")):
            return True
        return any(
            _is_subscriptions_csv(f) for f in path.glob("**/subscriptions/subscriptions.csv")
        )

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from self._parse_youtube_history(root)
        yield from self._parse_subscriptions(root)
        yield from self._parse_chat(root)

    # -- YouTube ------------------------------------------------------------

    def _parse_youtube_history(self, root: Path) -> Iterator[Batch]:
        histories = [
            ("watch", "**/YouTube and YouTube Music/history/watch-history.json"),
            ("search", "**/YouTube and YouTube Music/history/search-history.json"),
        ]
        for kind, pattern in histories:
            for history_file in sorted(root.glob(pattern)):
                try:
                    data = load_json(history_file)
                except Exception:
                    continue
                if not isinstance(data, list):
                    continue
                records: list[NormalizedRecord] = []
                seen: dict[str, int] = {}
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    subtitles = entry.get("subtitles") or []
                    channel = None
                    if subtitles and isinstance(subtitles[0], dict):
                        channel = subtitles[0].get("name")
                    ts = entry.get("time")
                    records.append(
                        NormalizedRecord(
                            connector=self.id,
                            type=RecordType.OTHER,
                            # Takeout returns history newest-first, so an
                            # index would renumber everything on every export.
                            uid=stable_uid(
                                "yt", kind, ts, entry.get("titleUrl"),
                                entry.get("title"), seen=seen,
                            ),
                            created_at=ts,
                            text=entry.get("title"),
                            author=channel,
                            extra={"kind": f"youtube_{kind}", "url": entry.get("titleUrl")},
                        )
                    )
                if records:
                    yield Batch(records=records)

    def _parse_subscriptions(self, root: Path) -> Iterator[Batch]:
        for subs_file in sorted(root.glob("**/subscriptions/subscriptions.csv")):
            if not _is_subscriptions_csv(subs_file):
                continue
            try:
                with subs_file.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                    rows = list(csv.DictReader(fh))
            except OSError:
                continue
            records = []
            seen: dict[str, int] = {}
            for row in rows:
                channel_id = (row.get("Channel Id") or "").strip()
                title = (row.get("Channel Title") or "").strip()
                if not channel_id and not title:
                    continue
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.FOLLOWING,
                        uid=stable_uid("yt:sub", channel_id or title, seen=seen),
                        text=title or None,
                        extra={"channel_url": (row.get("Channel Url") or "").strip() or None},
                    )
                )
            if records:
                yield Batch(records=records)

    # -- Google Chat --------------------------------------------------------

    def _parse_chat(self, root: Path) -> Iterator[Batch]:
        for messages_file in sorted(root.glob("**/Google Chat/Groups/**/messages.json")):
            try:
                data = load_json(messages_file)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            group = messages_file.parent.name
            records = []
            seen: dict[str, int] = {}
            for msg in data.get("messages", []):
                if not isinstance(msg, dict):
                    continue
                creator = msg.get("creator") or {}
                if not isinstance(creator, dict):
                    creator = {}
                raw_date = msg.get("created_date")
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.MESSAGE,
                        uid=stable_uid(
                            "chat", group, raw_date, creator.get("email"),
                            msg.get("text"), seen=seen,
                        ),
                        created_at=_chat_iso(raw_date),
                        text=msg.get("text"),
                        author=creator.get("name"),
                        thread=group,
                        extra={"email": creator.get("email"), "created_date_raw": raw_date},
                    )
                )
            if records:
                yield Batch(records=records)


register(GoogleConnector())
