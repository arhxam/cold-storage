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
from ..textutil import sqlite_safe

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
        # A concurrent reader (e.g. `cold serve`) and writer (`cold ingest`) may
        # run as separate processes; wait a bit instead of "database is locked".
        self._conn.execute("PRAGMA busy_timeout=5000")
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
                sqlite_safe(record.author),
                sqlite_safe(record.thread),
                sqlite_safe(record.text),
                json.dumps(d["media"]),
                json.dumps(d["extra"], ensure_ascii=True),
            ),
        )
        return True

    def attach_media(self, global_uid: str, shas: list[str]) -> bool:
        """Add media hashes to a record that already exists. Returns True if changed."""
        import json

        row = self._conn.execute(
            "SELECT media FROM records WHERE global_uid=?", (global_uid,)
        ).fetchone()
        if row is None:
            return False
        try:
            current = json.loads(row["media"] or "[]")
        except (TypeError, ValueError):
            current = []
        merged = list(dict.fromkeys([*current, *shas]))
        if merged == current:
            return False
        self._conn.execute(
            "UPDATE records SET media=? WHERE global_uid=?", (json.dumps(merged), global_uid)
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
        """Full-text search.

        People type what they remember — "why?", "AT&T", a half-typed quote —
        and FTS5 reads most punctuation as query syntax. Anything unquoted
        raised OperationalError straight out of the CLI and the web handler, so
        the query is passed as quoted phrase tokens instead: punctuation is
        searched for literally rather than parsed.
        """
        match = _fts_phrase(query)
        if not match:
            return []
        sql = (
            "SELECT r.* FROM records_fts f JOIN records r ON r.rowid=f.rowid "
            "WHERE records_fts MATCH ?"
        )
        params: list = [match]
        if connector:
            sql += " AND r.connector=?"
            params.append(connector)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            with closing(self._conn.execute(sql, params)) as cur:
                return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            # Anything the tokenizer still refuses is "no matches", never a crash.
            return []

    def last_run(self, connector: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE connector=? ORDER BY id DESC LIMIT 1", (connector,)
        ).fetchone()
        return dict(row) if row else None

    def records_page(
        self,
        *,
        connector: str | None = None,
        type_: str | None = None,
        limit: int = 300,
    ) -> list[dict]:
        """Records for one collection, filtered in SQL.

        This used to read a global slice and filter it in Python, so once an
        alphabetically-earlier connector had more rows than the slice, every
        later connector's collections came back empty — the sidebar said
        "Followers · 123" and the page said "Nothing here".
        """
        where, params = [], []
        if connector:
            where.append("connector=?")
            params.append(connector)
        if type_:
            where.append("type=?")
            params.append(type_)
        sql = "SELECT connector, type, uid, created_at, author, thread, text, media FROM records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, thread LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def iter_records(self, *, limit: int | None = None) -> list[dict]:
        """Return all records (optionally capped), oldest-ish first, for the viewer."""
        sql = (
            "SELECT connector, type, uid, created_at, author, thread, text, media "
            "FROM records ORDER BY connector, thread, created_at"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def self_author(self, connector: str) -> str | None:
        """Best guess at the account owner: the sender present in the most threads.

        In every real export, "you" are the one participant who appears in all of
        your own conversations — so the author spanning the most distinct threads
        is almost certainly you. Used to right-align your own chat bubbles.
        """
        row = self._conn.execute(
            """SELECT author, COUNT(DISTINCT thread) d FROM records
               WHERE connector=? AND type='message' AND author IS NOT NULL
               GROUP BY author ORDER BY d DESC, COUNT(*) DESC LIMIT 1""",
            (connector,),
        ).fetchone()
        return row["author"] if row else None

    def threads(self, connector: str) -> list[dict]:
        """Conversation list for a connector: newest activity first."""
        counts = self._conn.execute(
            """SELECT thread, COUNT(*) c, MAX(created_at) last_at
               FROM records
               WHERE connector=? AND type='message' AND thread IS NOT NULL
               GROUP BY thread ORDER BY last_at DESC""",
            (connector,),
        ).fetchall()
        last = self._conn.execute(
            """SELECT r.thread, r.text, r.author FROM records r
               JOIN (SELECT thread, MAX(created_at) m FROM records
                     WHERE connector=? AND type='message' GROUP BY thread) t
               ON r.thread=t.thread AND r.created_at=t.m
               WHERE r.connector=? AND r.type='message'""",
            (connector, connector),
        ).fetchall()
        preview = {r["thread"]: (r["text"], r["author"]) for r in last}
        out = []
        for row in counts:
            text, author = preview.get(row["thread"], (None, None))
            out.append(
                {
                    "thread": row["thread"],
                    "count": row["c"],
                    "last_at": row["last_at"],
                    "last_text": text,
                    "last_author": author,
                }
            )
        return out

    def thread_messages(self, connector: str, thread: str, limit: int = 2000) -> list[dict]:
        """The most recent ``limit`` messages, in reading order.

        Taking the *first* N meant a long conversation stopped years ago: the
        list preview showed the real latest message and the transcript below it
        ended somewhere else entirely.
        """
        rows = self._conn.execute(
            """SELECT * FROM records
               WHERE connector=? AND type='message' AND thread=?
               ORDER BY created_at DESC LIMIT ?""",
            (connector, thread, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def thread_message_count(self, connector: str, thread: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM records WHERE connector=? AND type='message' AND thread=?",
            (connector, thread),
        ).fetchone()
        return row["c"] if row else 0

    def records_for_type(self, connector: str, type_: RecordType, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE connector=? AND type=? ORDER BY created_at LIMIT ?",
            (connector, type_.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _fts_phrase(query: str) -> str:
    """Turn a human query into a safe FTS5 MATCH expression.

    Each whitespace-separated word becomes a quoted phrase, so `?`, `:`, `-`,
    `*`, `NEAR(` and unbalanced quotes are matched as text instead of being
    interpreted. Words are ANDed, which is what people expect from a search box.
    """
    words = [w for w in (query or "").split() if w.strip()]
    quoted = []
    for w in words:
        # Keep only characters the tokenizer can index; a token that reduces to
        # nothing (e.g. "!!!") is dropped rather than producing invalid syntax.
        cleaned = "".join(c if c.isalnum() or c in "_'" else " " for c in w).strip()
        for part in cleaned.split():
            quoted.append('"' + part.replace('"', '""') + '"')
    return " ".join(quoted)
