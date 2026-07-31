"""Snapchat — Rail A (official "My Data" export parser).

Handles chat history, Memories, and the friends list from Snapchat's
``json/`` folder. Real exports vary between two chat_history shapes
("Received Saved Chats"/"Sent Saved Chats" buckets vs. per-conversation
keys), so parsing is deliberately defensive: any top-level list of dicts
with ``From``/``Created``/``Text`` fields is treated as chat messages.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, load_json, register

_MARKERS = ("chat_history.json", "memories_history.json")


class SnapchatConnector(Connector):
    id = "snapchat"
    display_name = "Snapchat"
    rail = "export"
    provides = ["messages", "media", "followers"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        return any(
            (path / "json" / m).exists() or list(path.glob(f"**/{m}")) for m in _MARKERS
        )

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        for chat_file in _find(root, "chat_history.json"):
            batch = self._parse_chats(chat_file)
            if batch.records:
                yield batch
        for memories_file in _find(root, "memories_history.json"):
            batch = self._parse_memories(memories_file)
            if batch.records:
                yield batch
        for friends_file in _find(root, "friends.json"):
            batch = self._parse_friends(friends_file)
            if batch.records:
                yield batch

    def _parse_chats(self, chat_file: Path) -> Batch:
        data = load_json(chat_file)
        records: list[NormalizedRecord] = []
        if not isinstance(data, dict):
            return Batch()
        # Two known shapes, handled identically: either the keys are
        # conversation partners, or buckets like "Received Saved Chats" whose
        # items carry a "Conversation Title" (or From/To) instead.
        for key, items in data.items():
            if not isinstance(items, list):
                continue
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                author = item.get("From")
                thread = item.get("Conversation Title") or key
                # Current exports store text in "Content"; older (~2022) exports
                # used "Text". Support both.
                text = item.get("Content")
                if text is None:
                    text = item.get("Text")
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.MESSAGE,
                        uid=f"chat:{key}:{i}",
                        created_at=_normalize_ts(item.get("Created")),
                        text=text,
                        author=author,
                        thread=thread,
                        extra={
                            k: v
                            for k, v in (
                                ("media_type", item.get("Media Type")),
                                ("to", item.get("To")),
                                ("is_sender", item.get("IsSender")),
                                ("media_ids", item.get("Media IDs")),
                            )
                            if v is not None
                        },
                    )
                )
        return Batch(records=records)

    def _parse_memories(self, memories_file: Path) -> Batch:
        # NOTE: Memories "Download Link" URLs are time-limited — Snapchat
        # expires them roughly 7 days after the export is generated, so they
        # must be fetched promptly or re-requested via a fresh export.
        data = load_json(memories_file)
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        items = data.get("Saved Media", []) if isinstance(data, dict) else []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("Media Type", ""))
            uid = f"memory:{item.get('Date')}:{i}"
            records.append(
                NormalizedRecord(
                    connector=self.id,
                    type=RecordType.MEDIA,
                    uid=uid,
                    created_at=_normalize_ts(item.get("Date")),
                    extra={"media_type": media_type} if media_type else {},
                )
            )
            url = item.get("Download Link")
            if url:
                media.append(
                    MediaRef(
                        owner_uid=uid,
                        kind="video" if "video" in media_type.lower() else "image",
                        source_url=url,
                    )
                )
        return Batch(records=records, media=media)

    def _parse_friends(self, friends_file: Path) -> Batch:
        data = load_json(friends_file)
        records: list[NormalizedRecord] = []
        items = data.get("Friends", []) if isinstance(data, dict) else []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            username = item.get("Username")
            display_name = item.get("Display Name")
            records.append(
                NormalizedRecord(
                    connector=self.id,
                    type=RecordType.FOLLOWER,
                    uid=f"friend:{username or i}",
                    text=username,
                    extra={"display_name": display_name} if display_name else {},
                )
            )
        return Batch(records=records)


def _find(root: Path, name: str) -> list[Path]:
    """Locate ``json/<name>``, falling back to a recursive glob."""
    direct = root / "json" / name
    if direct.exists():
        return [direct]
    return sorted(root.glob(f"**/{name}"))


def _normalize_ts(value: object) -> str | None:
    """Normalize Snapchat's "2021-01-01 12:00:00 UTC" into ISO-8601 UTC.

    Anything that doesn't match that shape is returned as-is (as a string).
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(" UTC"):
        stripped = text[: -len(" UTC")]
        if len(stripped) == 19 and stripped[10] == " ":
            return stripped.replace(" ", "T", 1) + "+00:00"
        return stripped
    return text


register(SnapchatConnector())
