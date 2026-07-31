"""SQLite index over all normalized records, with FTS5 full-text search.

The index is a *derived* view: the source of truth is the raw exports + manifests.
It exists to make the archive searchable and browsable (the viewer reads it). It
can always be rebuilt from the manifests.

Note: record text may be private (chats). When encryption is enabled the index
lives inside the encrypted home tree; we do not additionally encrypt the SQLite
file itself in Phase 1 (documented limitation — see the design spec).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from ..models import NormalizedRecord, RecordType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    global_uid TEXT PRIMARY KEY,
    connector  TEXT NOT NULL,
    type       TEXT NOT NULL,
    uid        TEXT NOT NULL,
    created_at TEXT,
    author     TEXT,
    thread     TEXT,
    text       TEXT,
    media      TEXT,   -- json array of sha256
    extra      TEXT    -- json object
);
CREATE INDEX IF NOT EXISTS idx_records_connector ON records(connector);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(type);
CREATE INDEX IF NOT EXISTS idx_records_thread ON records(thread);
CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    text, author, thread,
    content='records', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, text, author, thread)
    VALUES (new.rowid, new.text, new.author, new.thread);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text, author, thread)
    VALUES ('delete', old.rowid, old.text, old.author, old.thread);
END;

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    connector  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status     TEXT NOT NULL,   -- ok | error
    added      INTEGER DEFAULT 0,
    error      TEXT
);
"""


class Index:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the read-only web app serve from a worker
        # thread; access is serialized by the single-threaded local server.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes --------------------------------------------------------------
    def upsert(self, record: NormalizedRecord) -> bool:
        """Insert a record. Returns True if newly added, False if it existed."""
        import json

        existing = self._conn.execute(
            "SELECT 1 FROM records WHERE global_uid=?", (record.global_uid,)
        ).fetchone()
        if existing:
            return False
        d = asdict(record)
        self._conn.execute(
            """INSERT INTO records
               (global_uid, connector, type, uid, created_at, author, thread, text, media, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                record.global_uid,
                record.connector,
                record.type.value,
                record.uid,
                record.created_at,
                record.author,
                record.thread,
                record.text,
                json.dumps(d["media"]),
                json.dumps(d["extra"]),
            ),
        )
        return True

    def upsert_many(self, records: list[NormalizedRecord]) -> int:
        added = 0
        for r in records:
            if self.upsert(r):
                added += 1
        self._conn.commit()
        return added

    def record_run(
        self,
        connector: str,
        started_at: str,
        finished_at: str,
        status: str,
        added: int,
        error: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO runs (connector, started_at, finished_at, status, added, error) "
            "VALUES (?,?,?,?,?,?)",
            (connector, started_at, finished_at, status, added, error),
        )
        self._conn.commit()

    # -- reads ---------------------------------------------------------------
    def count(self, connector: str | None = None) -> int:
        if connector:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM records WHERE connector=?", (connector,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) c FROM records").fetchone()
        return row["c"]

    def counts_by_connector(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT connector, COUNT(*) c FROM records GROUP BY connector"
        ).fetchall()
        return {r["connector"]: r["c"] for r in rows}

    def counts_by_type(self, connector: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT type, COUNT(*) c FROM records WHERE connector=? GROUP BY type", (connector,)
        ).fetchall()
        return {r["type"]: r["c"] for r in rows}

    def search(self, query: str, *, connector: str | None = None, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT r.* FROM records_fts f JOIN records r ON r.rowid=f.rowid "
            "WHERE records_fts MATCH ?"
        )
        params: list = [query]
        if connector:
            sql += " AND r.connector=?"
            params.append(connector)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with closing(self._conn.execute(sql, params)) as cur:
            return [dict(row) for row in cur.fetchall()]

    def last_run(self, connector: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE connector=? ORDER BY id DESC LIMIT 1", (connector,)
        ).fetchone()
        return dict(row) if row else None

    def iter_records(self, *, limit: int | None = None) -> list[dict]:
        """Return all records (optionally capped), oldest-ish first, for the viewer."""
        sql = (
            "SELECT connector, type, uid, created_at, author, thread, text, media "
            "FROM records ORDER BY connector, thread, created_at"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def records_for_type(self, connector: str, type_: RecordType, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE connector=? AND type=? ORDER BY created_at LIMIT ?",
            (connector, type_.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]
