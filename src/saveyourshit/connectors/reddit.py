"""Reddit — Rail A (GDPR "request your data" CSV export parser).

Reddit's export is a folder of CSV files (comments.csv, posts.csv, saved_*.csv,
message_*.csv, …). We parse the common ones defensively by column name.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch, NormalizedRecord, RecordType
from .base import Connector, register


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return (reader.fieldnames or []), rows


class RedditConnector(Connector):
    id = "reddit"
    display_name = "Reddit"
    rail = "export"
    provides = ["posts", "comments", "messages", "saved"]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        # Reddit exports ship many CSVs; look for the tell-tale combination of a
        # comments/posts CSV that has a `subreddit` column.
        for name in ("comments.csv", "posts.csv"):
            for f in [path / name, *path.glob(f"**/{name}")]:
                if f.exists():
                    try:
                        header, _ = _read_csv(f)
                    except Exception:
                        continue
                    if "subreddit" in header or "permalink" in header:
                        return True
        return False

    def parse_export(self, path: Path) -> Iterator[Batch]:
        root = Path(path)
        yield from self._parse(root, "comments.csv", RecordType.COMMENT, "body")
        yield from self._parse(root, "posts.csv", RecordType.POST, "title", body2="body")
        yield from self._parse(root, "saved_posts.csv", RecordType.SAVED, "permalink")
        yield from self._parse(root, "saved_comments.csv", RecordType.SAVED, "permalink")

    def _parse(
        self,
        root: Path,
        filename: str,
        type_: RecordType,
        text_col: str,
        *,
        body2: str | None = None,
    ) -> Iterator[Batch]:
        for f in [root / filename, *root.glob(f"**/{filename}")]:
            if not f.exists():
                continue
            _, rows = _read_csv(f)
            records = []
            for row in rows:
                rid = row.get("id") or row.get("permalink")
                if not rid:
                    continue
                text = row.get(text_col) or (row.get(body2) if body2 else None)
                records.append(
                    NormalizedRecord(
                        connector=self.id,
                        type=type_,
                        uid=f"{type_.value}:{rid}",
                        created_at=row.get("date"),
                        text=text,
                        thread=row.get("subreddit"),
                        extra={"permalink": row.get("permalink")},
                    )
                )
            if records:
                yield Batch(records=records)
            break  # only the first matching file per name


register(RedditConnector())
