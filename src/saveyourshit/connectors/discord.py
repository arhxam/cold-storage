"""Discord — Rail A (official data-package parser).

Note the honest limitation: Discord's official package contains only the messages
*you sent* — you get your half of every DM. Full both-sides capture would require
token automation, which risks an account ban (and Discord actively enforces it),
so this connector is export-only by design.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, register


class DiscordConnector(Connector):
    id = "discord"
    display_name = "Discord"
    rail = "export"
    provides = ["messages"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        return (path / "messages" / "index.json").exists() or bool(
            list(path.glob("**/messages/index.json"))
        )

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        index_files = list(root.glob("**/messages/index.json"))
        channel_names: dict[str, str] = {}
        if index_files:
            try:
                channel_names = json.loads(index_files[0].read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                channel_names = {}

        for channel_dir in sorted(root.glob("**/messages/c*/")):
            channel_id = channel_dir.name.lstrip("c")
            channel_name = channel_names.get(channel_id) or channel_id
            yield from self._parse_channel(channel_dir, channel_id, channel_name)

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
            mid = str(row.get("ID") or row.get("id") or "")
            if not mid:
                continue
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
