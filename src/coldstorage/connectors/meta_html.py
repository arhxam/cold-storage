"""Meta (Instagram / Facebook) HTML export parsing.

Meta's "Download your information" defaults to an HTML export, not JSON. Rather
than make the user re-request JSON, we read the HTML directly. Every section is
built from the same repeating record block::

    <div class="pam _3-95 _2ph- _a6-g uiBoxWhite noborder">
       <div class="_2pim _a6-h _a6-i">Maya</div>          # sender (messages)
       <div class="_a6-p"> Hey! did you see this  <img src="…/photos/x.jpg"> </div>
       <div class="_a72d">Jun 04, 2026 12:34 pm</div>      # timestamp (messages)
    </div>

Activity logs put an ISO timestamp in that same header slot instead. So the
generic extractor below pulls, per record: an ISO or human timestamp, the
remaining text lines (sender first, for messages), and any <img>/<a> media —
then the section parsers map those into the same NormalizedRecords the JSON
parsers emit. An HTML export becomes a first-class citizen.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import stable_uid

_VIDEO = {".mp4", ".mov", ".3gp", ".webm"}
_AUDIO = {".m4a", ".mp3", ".ogg", ".opus", ".aac", ".wav"}
_MEDIA_EXT = _VIDEO | _AUDIO | {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}

_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
# "Jun 04, 2026 12:34 pm" / "Jun 04, 2026 12:34:07 PM"
_HUMAN = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}[, ]+\d{1,2}:\d{2}(?::\d{2})?\s*[apAP]\.?\s?[mM]\.?$"
)


def _media_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    return "image"


def _is_media_uri(uri: str) -> bool:
    return Path(uri.split("?")[0]).suffix.lower() in _MEDIA_EXT


def _human_to_iso(s: str) -> str | None:
    s = s.strip().replace(".", "")
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return None


class _Record:
    __slots__ = ("texts", "media", "links", "ts")

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.media: list[str] = []
        self.links: list[str] = []
        self.ts: str | None = None


class _MetaHTMLParser(HTMLParser):
    """Segment a Meta HTML file into one record per ``uiBoxWhite`` block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[_Record] = []
        self._div_depth = 0
        self._rec: _Record | None = None
        self._rec_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "div":
            self._div_depth += 1
            if self._rec is None and "uiBoxWhite" in a.get("class", ""):
                self._rec = _Record()
                self._rec_depth = self._div_depth
        if self._rec is not None:
            src = a.get("src")
            if tag in ("img", "video", "source", "audio") and src and _is_media_uri(src):
                self._rec.media.append(src)
            href = a.get("href")
            if tag == "a" and href:
                if _is_media_uri(href):
                    self._rec.media.append(href)
                else:
                    self._rec.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._rec is not None and self._div_depth == self._rec_depth:
                self.records.append(self._rec)
                self._rec = None
                self._rec_depth = None
            self._div_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._rec is None:
            return
        t = data.strip()
        if not t:
            return
        if _ISO.match(t):
            # An ISO timestamp is the record's time, not content.
            try:
                self._rec.ts = datetime.fromisoformat(t).astimezone(UTC).isoformat()
            except ValueError:
                self._rec.ts = t
            return
        if _HUMAN.match(t):
            if self._rec.ts is None:
                self._rec.ts = _human_to_iso(t) or t
            return
        self._rec.texts.append(t)


def _records(path: Path) -> list[_Record]:
    parser = _MetaHTMLParser()
    parser.feed(Path(path).read_text(encoding="utf-8", errors="replace"))
    return parser.records


# Meta HTML media uris are rooted differently across export layouts; try each.
_MEDIA_ROOTS = ("", "your_instagram_activity", "your_facebook_activity", "your_activity_across_facebook")


def _resolve_media(root: Path, file_dir: Path, uri: str) -> str:
    uri = uri.split("?")[0]
    candidates = [file_dir / uri]
    candidates += [(root / prefix / uri) if prefix else (root / uri) for prefix in _MEDIA_ROOTS]
    for cand in candidates:
        try:
            if cand.exists():
                return str(cand)
        except OSError:
            continue
    return str(root / uri)


# ---------------------------------------------------------------------------
# Section parsers — each mirrors its JSON counterpart, but reads the HTML files.
# ---------------------------------------------------------------------------
def parse_html_message_threads(root: Path, connector: str) -> Iterator[Batch]:
    """One Batch per HTML message thread (``.../messages/<box>/<thread>/message_*.html``)."""
    root = Path(root)
    for msg_file in sorted(root.glob("**/messages/*/*/message_*.html")):
        recs = _records(msg_file)
        if not recs:
            continue
        thread = msg_file.parent.name
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        seen: dict[str, int] = {}
        for r in recs:
            # Messages put the sender name first; the rest is the message text.
            sender = r.texts[0] if r.texts else None
            content = " ".join(r.texts[1:]).strip() or None
            uris = ",".join(r.media)
            uid = stable_uid("msg", thread, r.ts, sender, content, uris or None, seen=seen)
            records.append(
                NormalizedRecord(
                    connector=connector,
                    type=RecordType.MESSAGE,
                    uid=uid,
                    created_at=r.ts,
                    text=content,
                    author=sender,
                    thread=thread,
                    extra={"thread_path": thread},
                )
            )
            for uri in r.media:
                media.append(
                    MediaRef(
                        owner_uid=uid,
                        kind=_media_kind(uri),
                        source_path=_resolve_media(root, msg_file.parent, uri),
                        filename=Path(uri).name,
                    )
                )
        if records:
            yield Batch(records=records, media=media)


def _parse_html_connections(
    root: Path, connector: str, pattern: str, rtype: RecordType
) -> Iterator[Batch]:
    for f in sorted(root.glob(pattern)):
        records: list[NormalizedRecord] = []
        seen: dict[str, int] = {}
        for r in _records(f):
            name = r.texts[0] if r.texts else None
            if not name and r.links:
                name = r.links[0].rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            uid = stable_uid(rtype.value, name, r.ts, seen=seen)
            records.append(
                NormalizedRecord(
                    connector=connector,
                    type=rtype,
                    uid=uid,
                    created_at=r.ts,
                    text=name,
                    author=name,
                    extra={"link": r.links[0] if r.links else None},
                )
            )
        if records:
            yield Batch(records=records)


def parse_html_followers(root: Path, connector: str) -> Iterator[Batch]:
    root = Path(root)
    yield from _parse_html_connections(
        root, connector, "**/followers_and_following/followers*.html", RecordType.FOLLOWER
    )
    yield from _parse_html_connections(
        root, connector, "**/followers_and_following/following.html", RecordType.FOLLOWING
    )
    # Facebook uses friends_and_followers/your_friends.html
    yield from _parse_html_connections(
        root, connector, "**/friends_and_followers/your_friends.html", RecordType.FOLLOWER
    )


def parse_html_posts(root: Path, connector: str) -> Iterator[Batch]:
    """Posts + reels from the HTML content section, with their media."""
    root = Path(root)
    globs = ["**/content/posts*.html", "**/media/posts*.html", "**/content/reels.html"]
    files: list[Path] = []
    for g in globs:
        files += list(root.glob(g))
    for f in sorted(set(files)):
        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []
        seen: dict[str, int] = {}
        for r in _records(f):
            if not r.texts and not r.media:
                continue
            caption = " ".join(r.texts).strip() or None
            uris = ",".join(r.media)
            uid = stable_uid("post", r.ts, caption, uris or None, seen=seen)
            records.append(
                NormalizedRecord(
                    connector=connector,
                    type=RecordType.POST,
                    uid=uid,
                    created_at=r.ts,
                    text=caption,
                )
            )
            for uri in r.media:
                media.append(
                    MediaRef(
                        owner_uid=uid,
                        kind=_media_kind(uri),
                        source_path=_resolve_media(root, f.parent, uri),
                        filename=Path(uri).name,
                    )
                )
        if records:
            yield Batch(records=records, media=media)
