"""Parsing shared by Instagram and Facebook (Meta uses one message format)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import load_json

_MEDIA_KEYS = {
    "photos": "image",
    "videos": "video",
    "audio_files": "audio",
    "files": "file",
    "gifs": "image",
}


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _sec_to_iso(sec: int | None) -> str | None:
    if not sec:
        return None
    try:
        return datetime.fromtimestamp(sec, tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _msg_uid(thread: str, ms: int | None, sender: str | None, content: str | None) -> str:
    h = hashlib.sha1(
        f"{thread}|{ms}|{sender}|{content}".encode("utf-8", "replace")
    ).hexdigest()[:20]
    return f"msg:{h}"


def parse_message_threads(root: Path, connector: str) -> Iterator[Batch]:
    """Yield one Batch per message thread found under ``root``.

    Matches Meta's ``.../messages/<folder>/<thread>/message_*.json`` layout
    (inbox, archived_threads, filtered_threads, e2ee, …).
    """
    root = Path(root)
    seen_files: set[Path] = set()
    for msg_file in sorted(root.glob("**/messages/*/*/message_*.json")):
        if msg_file in seen_files:
            continue
        seen_files.add(msg_file)
        data = load_json(msg_file, fix_mojibake=True)
        if not isinstance(data, dict):
            continue
        thread = str(data.get("thread_path") or msg_file.parent.name)
        title = data.get("title")
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        for msg in data.get("messages", []):
            if not isinstance(msg, dict):
                continue
            ms = msg.get("timestamp_ms")
            sender = msg.get("sender_name")
            content = msg.get("content")
            uid = _msg_uid(thread, ms, sender, content)
            rec = NormalizedRecord(
                connector=connector,
                type=RecordType.MESSAGE,
                uid=uid,
                created_at=_ms_to_iso(ms),
                text=content,
                author=sender,
                thread=title or thread,
                extra={"thread_path": thread},
            )
            for key, kind in _MEDIA_KEYS.items():
                for item in msg.get(key, []) or []:
                    uri = item.get("uri") if isinstance(item, dict) else None
                    if not uri:
                        continue
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind=kind,
                            source_path=str(root / uri),
                            filename=Path(uri).name,
                        )
                    )
            records.append(rec)
        if records:
            yield Batch(records=records, media=media)


def parse_followers_list(
    path: Path, connector: str, type_: RecordType, *, key: str | None = None
) -> Batch:
    """Parse a Meta followers/following JSON file into follow records.

    Handles both shapes Meta emits: a bare top-level list, or a dict wrapping the
    list under ``key`` (e.g. ``relationships_following``).
    """
    data = load_json(path, fix_mojibake=True)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get(key, [])
    else:
        items = []
    records: list[NormalizedRecord] = []
    for entry in items:
        sld = entry.get("string_list_data", []) if isinstance(entry, dict) else []
        if not sld:
            continue
        info = sld[0]
        value = info.get("value") or info.get("href") or ""
        if not value:
            continue
        records.append(
            NormalizedRecord(
                connector=connector,
                type=type_,
                uid=f"{type_.value}:{value}",
                created_at=_sec_to_iso(info.get("timestamp")),
                text=value,
                author=value,
                extra={"href": info.get("href")},
            )
        )
    return Batch(records=records)
