"""Discord — Rail A (official data-package parser).

Note the honest limitation: Discord's official package contains only the messages
*you sent* — you get your half of every DM. Full both-sides capture would require
token automation, which risks an account ban (and Discord actively enforces it),
so this connector is export-only by design.

Package layout notes (verified against real exports):

- Around December 2025 Discord renamed the ``messages/`` folder to ``Messages/``
  (capital M), so both spellings must be matched case-insensitively.
- ``Messages/index.json`` is a flat ``{"<channel_id>": "name or null"}`` map;
  null values are deleted channels.
- Channel folders are ``c<channel_id>/`` (packages after 2021-12-06) or bare
  ``<channel_id>/`` (older) — the ``c`` prefix is optional.
- Each channel folder holds ``channel.json`` plus ``messages.json`` (a JSON list
  of ``{"ID", "Timestamp", "Contents", "Attachments"}``); legacy packages use
  ``messages.csv`` with the same column headers. ``ID`` may be a JSON number or
  a string.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, register

# Channel folders: "c<id>" (post-2021 packages) or bare "<id>" (older ones).
_CHANNEL_DIR_RE = re.compile(r"^c?(\d+)$")


def _find_messages_dirs(root: Path) -> list[Path]:
    """All directories named ``messages``/``Messages`` (case-insensitive)."""
    dirs = []
    if root.is_dir() and root.name.lower() == "messages":
        dirs.append(root)
    dirs.extend(
        p for p in root.rglob("*") if p.is_dir() and p.name.lower() == "messages"
    )
    return sorted(set(dirs))


def _channel_dirs(messages_dir: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(channel_dir, channel_id)`` for every channel folder."""
    for child in sorted(messages_dir.iterdir()):
        if not child.is_dir():
            continue
        m = _CHANNEL_DIR_RE.match(child.name)
        if m:
            yield child, m.group(1)


class DiscordConnector(Connector):
    id = "discord"
    display_name = "Discord"
    rail = "export"
    provides = ["messages"]

    def detect(self, path: Path) -> bool:
        for messages_dir in _find_messages_dirs(Path(path)):
            if (messages_dir / "index.json").exists():
                return True
            if any(True for _ in _channel_dirs(messages_dir)):
                return True
        return False

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        for messages_dir in _find_messages_dirs(root):
            channel_names = self._load_index(messages_dir / "index.json")
            for channel_dir, channel_id in _channel_dirs(messages_dir):
                channel_name = channel_names.get(channel_id) or channel_id
                yield from self._parse_channel(channel_dir, channel_id, channel_name)

    @staticmethod
    def _load_index(index_file: Path) -> dict[str, str]:
        """Load index.json, skipping null names (deleted channels)."""
        if not index_file.exists():
            return {}
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, str)}

    def _parse_channel(self, channel_dir: Path, channel_id: str, name: str) -> Iterator[Batch]:
        json_file = channel_dir / "messages.json"
        csv_file = channel_dir / "messages.csv"
        rows: list[dict] = []
        if json_file.exists():
            data = json.loads(json_file.read_text(encoding="utf-8"))
            rows = data if isinstance(data, list) else []
        elif csv_file.exists():
            with csv_file.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        if not rows:
            return

        records, media = [], []
        for row in rows:
            raw_id = row.get("ID", row.get("id"))
            if raw_id is None or raw_id == "":
                continue
            mid = str(raw_id)
            uid = f"msg:{channel_id}:{mid}"
            records.append(
                NormalizedRecord(
                    connector=self.id,
                    type=RecordType.MESSAGE,
                    uid=uid,
                    created_at=row.get("Timestamp") or row.get("timestamp"),
                    text=row.get("Contents") or row.get("contents"),
                    thread=name,
                    extra={"channel_id": channel_id},
                )
            )
            attachments = row.get("Attachments") or row.get("attachments") or ""
            for url in _split_attachments(attachments):
                media.append(MediaRef(owner_uid=uid, kind="file", source_url=url))
        if records:
            yield Batch(records=records, media=media)


def _split_attachments(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [p for p in str(value).split() if p.startswith("http")]


register(DiscordConnector())
