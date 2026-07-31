"""X / Twitter — Rail A (official archive parser).

The Twitter archive stores data as JavaScript files: ``window.YTD.<name>.part0 =
[ ... ]``. We strip the assignment prefix and parse the JSON array. Covers
tweets, DMs, followers, and following.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..models import Batch, NormalizedRecord, RecordType
from .base import Connector, register

_ASSIGN = re.compile(r"^\s*window\.YTD\.[A-Za-z0-9_]+\.part\d+\s*=\s*", re.DOTALL)


def load_ytd_js(path: Path) -> list:
    """Parse a Twitter ``.js`` archive file into its JSON array."""
    text = Path(path).read_text(encoding="utf-8")
    stripped = _ASSIGN.sub("", text, count=1)
    data = json.loads(stripped)
    return data if isinstance(data, list) else []


def _twitter_ts(created_at: str | None) -> str | None:
    if not created_at:
        return None
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return created_at  # some archives already use ISO


class TwitterConnector(Connector):
    id = "twitter"
    display_name = "X (Twitter)"
    rail = "export"
    provides = ["tweets", "messages", "followers", "following"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        for name in ("tweets.js", "tweet.js", "direct-messages.js"):
            if (path / "data" / name).exists() or list(path.glob(f"**/data/{name}")):
                return True
        return False

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from self._parse_tweets(root)
        yield from self._parse_dms(root)
        yield from self._parse_follows(root, "follower.js", "follower", RecordType.FOLLOWER)
        yield from self._parse_follows(root, "following.js", "following", RecordType.FOLLOWING)

    def _find(self, root: Path, name: str) -> list[Path]:
        """Locate an export file, including its multi-part siblings.

        X splits large archives: ``tweets.js`` is joined by ``tweets-part1.js``,
        ``tweets-part2.js`` and so on. Matching the exact name meant everything
        after the first part was silently missing from the backup.
        """
        stem, _, ext = name.rpartition(".")
        pattern = f"{stem}*.{ext}" if stem else name
        found = sorted(set(root.glob(f"**/data/{pattern}"))) or sorted(
            set(root.glob(f"**/{pattern}"))
        )
        return found

    def _parse_tweets(self, root: Path) -> Iterator[Batch]:
        for f in self._find(root, "tweets.js") + self._find(root, "tweet.js"):
            records = []
            for entry in load_ytd_js(f):
                tw = entry.get("tweet", entry) if isinstance(entry, dict) else {}
                tid = tw.get("id_str") or tw.get("id")
                if not tid:
                    continue
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=RecordType.POST,
                        uid=f"tweet:{tid}",
                        created_at=_twitter_ts(tw.get("created_at")),
                        text=tw.get("full_text") or tw.get("text"),
                    )
                )
            if records:
                yield Batch(records=records)

    def _parse_dms(self, root: Path) -> Iterator[Batch]:
        for f in self._find(root, "direct-messages.js"):
            records = []
            for convo in load_ytd_js(f):
                dm = convo.get("dmConversation", {}) if isinstance(convo, dict) else {}
                conv_id = dm.get("conversationId", "")
                for msg in dm.get("messages", []):
                    mc = msg.get("messageCreate") if isinstance(msg, dict) else None
                    if not mc:
                        continue
                    mid = mc.get("id")
                    records.append(
                        NormalizedRecord(
                            connector=self.id,
                            type=RecordType.MESSAGE,
                            uid=f"dm:{mid}",
                            created_at=mc.get("createdAt"),
                            text=mc.get("text"),
                            author=mc.get("senderId"),
                            thread=conv_id,
                        )
                    )
            if records:
                yield Batch(records=records)

    def _parse_follows(self, root: Path, filename: str, key: str, type_) -> Iterator[Batch]:
        for f in self._find(root, filename):
            records = []
            for entry in load_ytd_js(f):
                obj = entry.get(key, {}) if isinstance(entry, dict) else {}
                acct = obj.get("accountId")
                if not acct:
                    continue
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=type_,
                        uid=f"{key}:{acct}",
                        text=acct,
                        author=acct,
                        extra={"link": obj.get("userLink")},
                    )
                )
            if records:
                yield Batch(records=records)


register(TwitterConnector())
