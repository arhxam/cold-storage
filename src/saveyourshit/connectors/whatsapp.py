"""WhatsApp — Rail A (exported ``_chat.txt`` parser).

Parses the plain-text chat export WhatsApp produces from Chat → Export chat.
Handles the iOS bracket format and the Android dash format in their real-world
shapes: locale-ordered dates (US ``M/D/YY``, EU ``D/M/YYYY``, German ``D.M.YY``),
12h and 24h times (including the U+202F narrow no-break space before AM/PM and
``p.m.`` spelled with periods), stray LRM/RLM/BOM marks, CRLF files, multi-line
messages, and every media marker WhatsApp emits (``<Media omitted>``,
``X.jpg (file attached)``, ``<attached: X.jpg>``, ``image omitted``, …).
Fully local — never touches WhatsApp's servers, so there is zero ban risk.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, register

# Invisible marks WhatsApp sprinkles into exports: LRM, RLM, and a possible BOM.
_MARKS = "\u200e\u200f\ufeff"  # LRM, RLM, BOM

# A real-world export timestamp: locale-ordered date + 12h/24h time.
#   1/29/18, 12:24:03   29.01.2018, 14:07   2021-01-01, 12:00:00   1/29/18, 6:24 PM
_TS = (
    r"\d{1,4}[-/.]\d{1,4}[-/.]\d{1,4}"  # date, any component order/separator
    r"(?:,\s*|\s+)"
    r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?"  # time, seconds optional
    r"(?:[\s\u202f\u00a0]?[APap]\.?[\s\u202f\u00a0]?[Mm]\.?)?"  # PM / p.m. / NNBSP+AM
)
# iOS:  [1/29/18, 12:24:03] Alice: hello
_IOS = re.compile(rf"^\[(?P<date>{_TS})\]\s(?P<rest>.*)$")
# Android:  1/29/18, 12:24 - Alice: hello   (separator " - ", no brackets)
_ANDROID = re.compile(rf"^(?P<date>{_TS})\s-\s(?P<rest>.*)$")

# Android "include media" marker:  IMG-20210428-WA0001.jpg (file attached)
_ATTACHED_ANDROID = re.compile(
    r"(?P<file>[\w.\-]+)\s*\((?:file attached|attached)\)", re.IGNORECASE
)
# iOS "include media" marker:  <attached: 00000035-PHOTO-2022-03-27-21-41-55.jpg>
_ATTACHED_IOS = re.compile(r"<attached:\s*(?P<file>[^<>]+?)\s*>", re.IGNORECASE)

# Timestamp components, matched after whitespace normalization.
_DT = re.compile(
    r"^(?P<a>\d{1,4})(?P<sep>[-/.])(?P<b>\d{1,4})(?P=sep)(?P<c>\d{1,4})"
    r"(?:,\s*|\s+)"
    r"(?P<h>\d{1,2})[:.](?P<mi>\d{2})(?:[:.](?P<sec>\d{2}))?"
    r"\s*(?P<ampm>[APap]\.?\s?[Mm]\.?)?\s*$"
)

_MEDIA_KINDS = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".heic": "image",
    ".mp4": "video",
    ".mov": "video",
    ".3gp": "video",
    ".opus": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
    ".ogg": "audio",
    ".aac": "audio",
}


def _media_kind(filename: str) -> str:
    return _MEDIA_KINDS.get(Path(filename).suffix.lower(), "file")


def _strip_marks(s: str) -> str:
    for mark in _MARKS:
        s = s.replace(mark, "")
    return s


def _parse_date(raw: str) -> str | None:
    """Parse a real-world export timestamp into ISO-8601.

    Handles US ``M/D/YY``, EU ``D/M/YYYY``, German ``D.M.YY``, and ISO year-first
    dates; 12h times (incl. narrow no-break space before AM/PM and ``p.m.`` with
    periods) and 24h times with optional seconds. Day/month order is disambiguated
    with a day>12 heuristic; ambiguous dotted dates default to day-first (the
    locales that use dots) and ambiguous slash dates to US month-first. If the
    timestamp still can't be parsed the raw string is kept so the message is
    never dropped.
    """
    s = _strip_marks(raw).replace("\u202f", " ").replace("\u00a0", " ").strip()
    m = _DT.match(s)
    if not m:
        return s or None
    a, b, c = int(m["a"]), int(m["b"]), int(m["c"])
    if a >= 1000:  # ISO-ish year-first: 2021-01-01
        year, month, day = a, b, c
    else:
        year = c if c >= 100 else 2000 + c
        if a > 12 >= b:  # day>12 → unambiguous day-first
            day, month = a, b
        elif b > 12 >= a:  # second component >12 → month-first
            month, day = a, b
        elif m["sep"] == ".":  # dotted dates are day-first in practice (German et al.)
            day, month = a, b
        else:  # ambiguous slash date: assume US month-first
            month, day = a, b
    hour, minute = int(m["h"]), int(m["mi"])
    second = int(m["sec"] or 0)
    ampm = (m["ampm"] or "").replace(".", "").replace(" ", "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        return s or None


def _thread_name(txt_file: Path) -> str:
    name = txt_file.stem
    m = re.match(r"WhatsApp Chat with (.+)", name, re.IGNORECASE)
    if m:
        return m.group(1)
    if name == "_chat":
        return txt_file.parent.name
    return name


class WhatsAppConnector(Connector):
    id = "whatsapp"
    display_name = "WhatsApp"
    rail = "export"
    provides = ["messages", "media"]

    def _chat_files(self, path: Path) -> list[Path]:
        files = list(path.glob("**/_chat.txt")) + list(path.glob("**/WhatsApp Chat*.txt"))
        if not files and path.is_file() and path.suffix == ".txt":
            files = [path]
        # dedupe preserving order
        seen, out = set(), []
        for f in files:
            if f not in seen:
                seen.add(f)
                out.append(f)
        return out

    def detect(self, path: Path) -> bool:
        return bool(self._chat_files(Path(path)))

    def parse_export(self, path: Path) -> Iterator[Batch]:
        for txt in self._chat_files(Path(path)):
            yield from self._parse_file(txt)

    def _parse_file(self, txt: Path) -> Iterator[Batch]:
        thread = _thread_name(txt)
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        idx = 0
        current: NormalizedRecord | None = None
        for raw in txt.read_text(encoding="utf-8", errors="replace").splitlines():
            # tolerate CRLF files and strip the LRM/RLM/BOM prefix real exports carry
            line = raw.rstrip("\r").lstrip(_MARKS)
            m = _IOS.match(line) or _ANDROID.match(line)
            if m:
                # LRM/RLM also appear mid-line (e.g. before "<attached: …>")
                rest = _strip_marks(m.group("rest"))
                if ": " in rest:
                    author, text = rest.split(": ", 1)
                else:
                    author, text = None, rest  # system message
                uid = f"msg:{thread}:{idx}"
                idx += 1
                current = NormalizedRecord(
                    connector=self.id,
                    type=RecordType.MESSAGE,
                    uid=uid,
                    created_at=_parse_date(m.group("date")),
                    text=text,
                    author=author,
                    thread=thread,
                )
                records.append(current)
                # "included media" markers → resolve the file next to the .txt.
                # Omitted markers (<Media omitted>, image omitted, …) match
                # neither pattern and simply stay as text.
                att = _ATTACHED_IOS.search(text) or _ATTACHED_ANDROID.search(text)
                if att:
                    fname = att.group("file").strip()
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind=_media_kind(fname),
                            source_path=str(txt.parent / fname),
                            filename=fname,
                        )
                    )
            elif current is not None:
                # continuation of a multi-line message
                current.text = (current.text or "") + "\n" + line
        if records:
            yield Batch(records=records, media=media)


register(WhatsAppConnector())
