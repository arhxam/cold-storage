"""Facebook + Messenger — Rail A (official export parser).

Shares Meta's message format with Instagram; adds friends and timeline posts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, load_json, register
from .meta_common import parse_message_threads


class FacebookConnector(Connector):
    id = "facebook"
    display_name = "Facebook & Messenger"
    rail = "export"
    provides = ["messages", "friends", "posts", "media"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        # Facebook-specific markers only. NOTE: do NOT use "personal_information"
        # — it also appears in Instagram exports and would mis-detect them.
        # "friends_and_followers" is FB-only (Instagram uses "followers_and_following").
        markers = [
            "your_facebook_activity",
            "your_activity_across_facebook",
            "friends_and_followers",
        ]
        return any((path / m).exists() or list(path.glob(f"**/{m}")) for m in markers)

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from parse_message_threads(root, self.id)
        yield from self._parse_friends(root)
        yield from self._parse_posts(root)

    def _parse_friends(self, root: Path) -> Iterator[Batch]:
        for friends_file in root.glob("**/your_friends.json"):
            data = load_json(friends_file, fix_mojibake=True)
            items = data.get("friends_v2", []) if isinstance(data, dict) else []
            records = []
            for entry in items:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not name:
                    continue
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.FOLLOWER,
                        uid=f"friend:{name}",
                        created_at=_iso(entry.get("timestamp")),
                        text=name,
                        author=name,
                        extra={"relation": "friend"},
                    )
                )
            if records:
                yield Batch(records=records)

    def _parse_posts(self, root: Path) -> Iterator[Batch]:
        for posts_file in root.glob("**/posts/your_posts*.json"):
            data = load_json(posts_file, fix_mojibake=True)
            entries = data if isinstance(data, list) else data.get("posts", [])
            records, media = [], []
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("timestamp")
                text = None
                for d in entry.get("data", []) or []:
                    if isinstance(d, dict) and "post" in d:
                        text = d["post"]
                        break
                text = text or entry.get("title")
                uid = f"post:{ts}:{i}"
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.POST,
                        uid=uid,
                        created_at=_iso(ts),
                        text=text,
                    )
                )
                for att in entry.get("attachments", []) or []:
                    for d in att.get("data", []) if isinstance(att, dict) else []:
                        m = d.get("media") if isinstance(d, dict) else None
                        uri = m.get("uri") if isinstance(m, dict) else None
                        if uri:
                            media.append(
                                MediaRef(
                                    owner_uid=uid,
                                    kind="video" if str(uri).lower().endswith(".mp4") else "image",
                                    source_path=str(root / uri),
                                    filename=Path(uri).name,
                                )
                            )
            if records:
                yield Batch(records=records, media=media)


def _iso(sec: int | None) -> str | None:
    if not sec:
        return None
    try:
        return datetime.fromtimestamp(sec, tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


register(FacebookConnector())
