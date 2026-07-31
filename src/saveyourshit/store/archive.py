"""The Archive facade: the one object connectors and the engine talk to.

It owns the blob store, the SQLite index, and per-connector manifests, and knows
how to ingest a :class:`Batch` (dedup media into blobs, rewrite media refs on
records, append to the manifest, index for search).
"""

from __future__ import annotations

from pathlib import Path

from ..crypto import Cipher
from ..models import Batch, NormalizedRecord
from ..paths import Layout
from .blobs import BlobStore
from .index import Index
from .manifest import Manifest


class Archive:
    def __init__(self, layout: Layout, cipher: Cipher | None = None) -> None:
        self.layout = layout.ensure()
        self.cipher = cipher
        self.blobs = BlobStore(layout.blobs_dir, cipher=cipher)
        self.index = Index(layout.index_db)

    def close(self) -> None:
        self.index.close()

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def manifest(self, connector: str) -> Manifest:
        return Manifest(self.layout.manifest_file(connector))

    def ingest_batch(self, batch: Batch) -> int:
        """Store a batch's media and records. Returns count of newly-added records.

        Idempotent: records already present (by ``global_uid``) are skipped, so
        re-ingesting the same export never duplicates.
        """
        # 1. resolve media into content-addressed blobs, keyed by owner record
        media_by_owner: dict[str, list[str]] = {}
        for m in batch.media:
            sha = self._store_media(m)
            if sha is None:
                continue
            m.sha256 = sha
            media_by_owner.setdefault(m.owner_uid, []).append(sha)

        # 2. attach resolved media shas onto their records
        fresh: list[NormalizedRecord] = []
        for r in batch.records:
            if r.uid in media_by_owner:
                for sha in media_by_owner[r.uid]:
                    if sha not in r.media:
                        r.media.append(sha)
            fresh.append(r)

        # 3. index (dedups by global_uid) and 4. append only the new ones to manifest
        by_connector: dict[str, list[NormalizedRecord]] = {}
        added_records: list[NormalizedRecord] = []
        for r in fresh:
            if self.index.upsert(r):
                added_records.append(r)
                by_connector.setdefault(r.connector, []).append(r)
        self.index._conn.commit()  # noqa: SLF001 — batch commit
        for connector, records in by_connector.items():
            self.manifest(connector).append_many(records)
        return len(added_records)

    def _store_media(self, media) -> str | None:
        if media.sha256 and self.blobs.has(media.sha256):
            return media.sha256
        if media.source_path:
            p = Path(media.source_path)
            if p.exists() and p.is_file():
                return self.blobs.put_file(p)
            return None
        # source_url handling (fetching remote media) is Phase 2 — see design spec.
        return None
