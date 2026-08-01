"""Telegram — Rail A (Telegram Desktop "Export Telegram data" parser).

Telegram Desktop exports a ``result.json`` (whole account, chats under
``chats.list``) or a single-chat JSON (top-level ``messages``). This is a clean,
officially-supported, zero-ban-risk export.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, load_json, register


def _extract_text(text) -> str | None:
    """Telegram ``text`` is a string, or a list of strings / {type,text} runs."""
    if text is None or text == "":
        return None
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for frag in text:
            if isinstance(frag, str):
                parts.append(frag)
            elif isinstance(frag, dict):
                parts.append(frag.get("text", ""))
        return "".join(parts) or None
    return None


class TelegramConnector(Connector):
    id = "telegram"
    display_name = "Telegram"
    rail = "export"
    provides = ["messages", "media"]

    def detect(self, path: Path) -> bool:
        for f in [path / "result.json", *path.glob("**/result.json")]:
            if f.exists():
                try:
                    data = load_json(f)
                except Exception:
                    continue
                if isinstance(data, dict) and (
                    "chats" in data or ("messages" in data and "type" in data)
                ):
                    return True
        return False

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        for f in [root / "result.json", *root.glob("**/result.json")]:
            if not f.exists():
                continue
            data = load_json(f)
            if not isinstance(data, dict):
                continue
            if "chats" in data:
                for chat in data.get("chats", {}).get("list", []):
                    yield from self._parse_chat(chat, f.parent)
            elif "messages" in data:
                yield from self._parse_chat(data, f.parent)
            break

    def _parse_chat(self, chat: dict, root: Path) -> Iterator[Batch]:
        name = chat.get("name") or str(chat.get("id") or "chat")
        records, media = [], []
        for msg in chat.get("messages", []):
            if not isinstance(msg, dict) or msg.get("type") != "message":
                continue
            mid = msg.get("id")
            thread_key = str(chat.get("id") or name)
            uid = f"msg:{thread_key}:{mid}"
            records.append(
                NormalizedRecord(
                    connector=self.id,
                    type=RecordType.MESSAGE,
                    uid=uid,
                    created_at=msg.get("date"),
                    text=_extract_text(msg.get("text")),
                    author=msg.get("from"),
                    thread=name,
                    extra={"from_id": msg.get("from_id")},
                )
            )
            for key, kind in (("photo", "image"), ("file", "file")):
                rel = msg.get(key)
                if rel:
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind=kind,
                            source_path=str(root / rel),
                            filename=Path(rel).name,
                        )
                    )
        if records:
            yield Batch(records=records, media=media)


register(TelegramConnector())
