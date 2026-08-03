"""Telegram Desktop HTML export parsing.

Telegram Desktop's "Export Telegram data" DEFAULTS to HTML (JSON is opt-in), so
without this a very common export reads as zero messages. It writes one folder
per chat under ``chats/chat_NNN/`` with ``messages.html`` (+ ``messages2.html`` …
for long chats). Each message looks like::

    <div class="message default clearfix" id="message123">
      <div class="body">
        <div class="pull_right date details" title="01.01.2024 12:34:56 UTC-05:00">…</div>
        <div class="from_name">Alice</div>
        <div class="text">Hello there</div>
        <div class="media_wrap clearfix"><a class="photo_wrap" href="photos/photo_1@…jpg">…</a></div>
      </div>
    </div>

Consecutive messages from the same sender are marked ``joined`` and omit
``from_name`` (they inherit the previous sender). ``message service`` rows
(joins, pins, calls) are skipped.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import stable_uid

_VIDEO = {".mp4", ".mov", ".webm"}
_AUDIO = {".ogg", ".oga", ".m4a", ".mp3", ".wav"}
_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _media_kind(name: str) -> str:
    ext = Path(name.split("?")[0]).suffix.lower()
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    if ext in _IMAGE:
        return "image"
    return "file"


# "01.01.2024 12:34:56 UTC-05:00"  ->  ISO-8601
_TG_DATE = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s*UTC([+-]\d{2}):?(\d{2})"
)
_TITLE = re.compile(r'title="([^"]*)"')
_FROM = re.compile(r'<div class="from_name">(.*?)</div>', re.S)
_TEXT = re.compile(r'<div class="text">(.*?)</div>', re.S)
_HEADER_NAME = re.compile(r'<div class="text bold">(.*?)</div>', re.S)
_MEDIA_HREF = re.compile(
    r'href="([^"]+\.(?:jpg|jpeg|png|gif|webp|mp4|mov|webm|ogg|oga|m4a|mp3|wav))"',
    re.I,
)
_TAGS = re.compile(r"<[^>]+>")


def _strip(fragment: str | None) -> str | None:
    if not fragment:
        return None
    text = _html.unescape(_TAGS.sub("", fragment)).strip()
    return text or None


def _iso(title: str | None) -> str | None:
    if not title:
        return None
    m = _TG_DATE.search(title)
    if not m:
        return None
    d, mo, y, hh, mm, ss, oh, om = m.groups()
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}{oh}:{om}"


def _chat_name(html_text: str, fallback: str) -> str:
    m = _HEADER_NAME.search(html_text)
    return _strip(m.group(1)) or fallback if m else fallback


def parse_html_chats(root: Path, connector: str) -> Iterator[Batch]:
    """One Batch per Telegram HTML chat folder."""
    root = Path(root)
    # Group the split message files (messages.html, messages2.html…) by chat dir.
    by_dir: dict[Path, list[Path]] = {}
    for f in sorted(root.glob("**/messages*.html")):
        if re.fullmatch(r"messages\d*\.html", f.name):
            by_dir.setdefault(f.parent, []).append(f)

    for chat_dir, files in by_dir.items():
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        seen: dict[str, int] = {}
        thread = chat_dir.name
        last_sender: str | None = None
        for f in sorted(files):
            text = f.read_text(encoding="utf-8", errors="replace")
            thread = _chat_name(text, chat_dir.name)
            # Each message is one sibling <div class="message …"> block; split
            # before each so a block holds exactly one message's fields.
            for block in re.split(r'(?=<div class="message )', text):
                if not block.startswith('<div class="message '):
                    continue
                cls_m = re.match(r'<div class="([^"]*)"', block)
                cls = (cls_m.group(1) if cls_m else "").split()
                if "service" in cls:
                    last_sender = None
                    continue
                sender = _strip(m.group(1)) if (m := _FROM.search(block)) else None
                if not sender and "joined" in cls:
                    sender = last_sender
                if sender:
                    last_sender = sender
                content = _strip(m.group(1)) if (m := _TEXT.search(block)) else None
                # date lives in the pull_right date-details div's title attribute
                ts = None
                date_tag = re.search(r'<div class="[^"]*date details"[^>]*>', block)
                if date_tag:
                    tm = _TITLE.search(date_tag.group(0))
                    ts = _iso(tm.group(1)) if tm else None
                uris = _MEDIA_HREF.findall(block)
                if not content and not uris:
                    continue  # nothing to store (sticker/poll/etc. with no text)
                uid = stable_uid("msg", thread, ts, sender, content, ",".join(uris) or None, seen=seen)
                records.append(
                    NormalizedRecord(
                        connector=connector,
                        type=RecordType.MESSAGE,
                        uid=uid,
                        created_at=ts,
                        text=content,
                        author=sender,
                        thread=thread,
                        extra={"thread_path": thread},
                    )
                )
                for uri in uris:
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind=_media_kind(uri),
                            source_path=str((chat_dir / uri)),
                            filename=Path(uri).name,
                        )
                    )
        if records:
            yield Batch(records=records, media=media)
