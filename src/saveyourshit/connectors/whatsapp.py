"""WhatsApp — Rail A (exported ``_chat.txt`` parser).

Parses the plain-text chat export WhatsApp produces from Chat → Export chat.
Handles the iOS bracket format and the Android dash format, multi-line messages,
and ``(file attached)`` media that sits next to the txt file. Fully local — never
touches WhatsApp's servers, so there is zero ban risk.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, register

# iOS:  [2021-01-01, 12:00:00] Alice: hello
_IOS = re.compile(r"^\[(?P<date>[^\]]+)\]\s(?P<rest>.*)$")
# Android:  1/1/21, 12:00 - Alice: hello   (optional seconds / am-pm)
_ANDROID = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4},\s\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\s-\s(?P<rest>.*)$"
)
_ATTACHED = re.compile(r"(?P<file>[\w.\-]+)\s*\((?:file attached|attached)\)", re.IGNORECASE)

_DATE_FORMATS = [
    "%Y-%m-%d, %H:%M:%S",
    "%m/%d/%y, %H:%M",
    "%d/%m/%y, %H:%M",
    "%m/%d/%Y, %H:%M",
    "%d/%m/%Y, %H:%M",
]


def _parse_date(s: str) -> str | None:
    s = s.strip().replace(" ", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


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
            line = raw.lstrip("‎‏")  # strip LTR/RTL marks WhatsApp inserts
            m = _IOS.match(line) or _ANDROID.match(line)
            if m:
                rest = m.group("rest")
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
                att = _ATTACHED.search(text)
                if att:
                    cand = txt.parent / att.group("file")
                    media.append(
                        MediaRef(
                            owner_uid=uid,
                            kind="file",
                            source_path=str(cand),
                            filename=att.group("file"),
                        )
                    )
            elif current is not None:
                # continuation of a multi-line message
                current.text = (current.text or "") + "\n" + line
        if records:
            yield Batch(records=records, media=media)


register(WhatsAppConnector())
