"""Instagram — Rail A (official "Download your information" export parser).

Handles DMs, followers, following, and posts (with media). Instagram's JSON is
mojibake-encoded; :func:`load_json(..., fix_mojibake=True)` repairs it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..models import Batch, MediaRef, NormalizedRecord, RecordType
from .base import Connector, load_json, register
from .meta_common import parse_followers_list, parse_message_threads


class InstagramConnector(Connector):
    id = "instagram"
    display_name = "Instagram"
    rail = "export"
    provides = ["messages", "followers", "following", "posts", "media"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        markers = ["your_instagram_activity", "connections/followers_and_following"]
        return any(
            (path / m).exists() or list(path.glob(f"**/{Path(m).name}")) for m in markers
        )

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from parse_message_threads(root, self.id)

        for followers_file in root.glob("**/followers_and_following/followers*.json"):
            yield parse_followers_list(followers_file, self.id, RecordType.FOLLOWER)
        for following_file in root.glob("**/followers_and_following/following.json"):
            yield parse_followers_list(
                following_file, self.id, RecordType.FOLLOWING, key="relationships_following"
            )

        yield from self._parse_posts(root)

    def _parse_posts(self, root: Path) -> Iterator[Batch]:
        for posts_file in list(root.glob("**/content/posts_*.json")) + list(
            root.glob("**/media/posts_*.json")
        ):
            data = load_json(posts_file, fix_mojibake=True)
            entries = data if isinstance(data, list) else data.get("posts", [])
            records: list[NormalizedRecord] = []
            media: list[MediaRef] = []
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                media_items = entry.get("media", [])
                ts = entry.get("creation_timestamp")
                if not ts and media_items:
                    ts = media_items[0].get("creation_timestamp")
                title = entry.get("title") or (media_items[0].get("title") if media_items else None)
                uid = f"post:{ts}:{i}"
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.POST,
                        uid=uid,
                        created_at=_iso(ts),
                        text=title,
                    )
                )
                for m in media_items:
                    uri = m.get("uri")
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


register(InstagramConnector())
