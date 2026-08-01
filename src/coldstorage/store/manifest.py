"""Append-only, diffable per-connector manifest (JSON Lines).

Every stored record is appended here as one JSON object per line. This is the
durable, human-readable, ``git diff``-able log of what has been backed up; the
SQLite index can always be rebuilt from these files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from ..models import NormalizedRecord, RecordType


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: NormalizedRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(record)
        d["type"] = record.type.value
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")

    def append_many(self, records: list[NormalizedRecord]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for record in records:
                d = asdict(record)
                d["type"] = record.type.value
                f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self) -> Iterator[NormalizedRecord]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                d["type"] = RecordType(d["type"])
                yield NormalizedRecord(**d)

    def seen_uids(self) -> set[str]:
        return {r.global_uid for r in self.read()}
