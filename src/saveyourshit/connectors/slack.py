"""Slack — Rail A (standard workspace export parser).

Parses an unpacked Slack workspace export: ``users.json`` / ``channels.json``
at the root, plus one folder per channel containing dated ``YYYY-MM-DD.json``
message files. Message text is kept verbatim, so Slack's raw markup survives
(``<@U123>`` mentions, ``<http://url|label>`` links).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, load_json, register

_DATED_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


class SlackConnector(Connector):
    id = "slack"
    display_name = "Slack"
    rail = "export"
    provides = ["messages"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if (path / "channels.json").exists() and (path / "users.json").exists():
            return True
        for channels_file in path.glob("**/channels.json"):
            if (channels_file.parent / "users.json").exists():
                return True
        return False

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = self._find_root(Path(path))
        if root is None:
            return
        users = self._load_users(root)
        for channel_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            yield from self._parse_channel(channel_dir, users)

    def _find_root(self, path: Path) -> Path | None:
        """Locate the directory holding ``channels.json`` + ``users.json``."""
        if (path / "channels.json").exists() and (path / "users.json").exists():
            return path
        for channels_file in sorted(path.glob("**/channels.json")):
            if (channels_file.parent / "users.json").exists():
                return channels_file.parent
        return None

    def _load_users(self, root: Path) -> dict[str, str]:
        """Map user id -> best display name (display > real > name > id)."""
        users: dict[str, str] = {}
        try:
            data = load_json(root / "users.json")
        except (OSError, ValueError):
            return users
        if not isinstance(data, list):
            return users
        for u in data:
            if not isinstance(u, dict) or not u.get("id"):
                continue
            profile = u.get("profile") or {}
            name = (
                (profile.get("display_name") if isinstance(profile, dict) else None)
                or u.get("real_name")
                or u.get("name")
            )
            if name:
                users[u["id"]] = str(name)
        return users

    def _parse_channel(self, channel_dir: Path, users: dict[str, str]) -> Iterator[Batch]:
        channel = channel_dir.name
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        for day_file in sorted(channel_dir.iterdir()):
            if not day_file.is_file() or not _DATED_FILE.match(day_file.name):
                continue
            try:
                messages = load_json(day_file)
            except (OSError, ValueError):
                continue
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if not isinstance(msg, dict) or msg.get("type") != "message":
                    continue
                ts = msg.get("ts")
                if not ts:
                    continue
                uid = f"msg:{channel}:{ts}"
                user_id = msg.get("user")
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.MESSAGE,
                        uid=uid,
                        created_at=_iso(ts),
                        text=msg.get("text"),
                        author=users.get(user_id, user_id) if user_id else None,
                        thread=channel,
                        extra={"channel": channel, "ts": str(ts)},
                    )
                )
                for f in msg.get("files") or []:
                    if not isinstance(f, dict):
                        continue
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind="file",
                            source_url=f.get("url_private"),
                            filename=f.get("name"),
                        )
                    )
        if records:
            yield Batch(records=records, media=media)


def _iso(ts: str | float | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


register(SlackConnector())
