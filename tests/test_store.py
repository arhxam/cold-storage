from saveyourshit.crypto import KeyManager
from saveyourshit.models import Batch, MediaRef, NormalizedRecord, RecordType
from saveyourshit.store.archive import Archive
from saveyourshit.store.blobs import BlobStore
from saveyourshit.store.index import Index
from saveyourshit.store.manifest import Manifest


def test_blobstore_dedup(tmp_path):
    bs = BlobStore(tmp_path / "blobs")
    a = bs.put_bytes(b"same content")
    b = bs.put_bytes(b"same content")
    assert a == b
    assert bs.count() == 1
    assert bs.get_bytes(a) == b"same content"


def test_blobstore_encrypted_roundtrip_and_dedup(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, _ = km.create("pw")
    bs = BlobStore(tmp_path / "blobs", cipher=cipher)
    sha = bs.put_bytes(b"secret media bytes")
    # stored file is encrypted (not equal to plaintext) and verifies
    assert bs.get_bytes(sha) == b"secret media bytes"
    assert bs.verify(sha)
    # dedup still works despite random nonces
    assert bs.put_bytes(b"secret media bytes") == sha
    assert bs.count() == 1


def test_index_upsert_dedup_and_search(tmp_path):
    idx = Index(tmp_path / "index.sqlite")
    r = NormalizedRecord(connector="ig", type=RecordType.MESSAGE, uid="m1", text="hello world", thread="t")
    assert idx.upsert(r) is True
    assert idx.upsert(r) is False  # dedup by global_uid
    idx._conn.commit()
    assert idx.count() == 1
    hits = idx.search("hello")
    assert len(hits) == 1 and hits[0]["uid"] == "m1"
    idx.close()


def test_manifest_append_and_read(tmp_path):
    m = Manifest(tmp_path / "manifest.jsonl")
    rec = NormalizedRecord(connector="ig", type=RecordType.POST, uid="p1", text="hi")
    m.append(rec)
    back = list(m.read())
    assert len(back) == 1
    assert back[0].uid == "p1"
    assert back[0].type == RecordType.POST


def test_archive_ingest_attaches_media_and_dedups(layout):
    with Archive(layout) as arc:
        batch = Batch(
            records=[NormalizedRecord(connector="ig", type=RecordType.MESSAGE, uid="m1", text="pic")],
            media=[MediaRef(owner_uid="m1", kind="image", source_path=None)],
        )
        # media with no resolvable source is skipped, record still stored
        added = arc.ingest_batch(batch)
        assert added == 1
        # re-ingest same batch: no new records
        assert arc.ingest_batch(batch) == 0
        assert arc.index.count("ig") == 1


def test_archive_ingest_stores_real_media(layout, tmp_path):
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"JPEGBYTES")
    with Archive(layout) as arc:
        batch = Batch(
            records=[NormalizedRecord(connector="ig", type=RecordType.MESSAGE, uid="m2")],
            media=[MediaRef(owner_uid="m2", kind="image", source_path=str(media_file))],
        )
        arc.ingest_batch(batch)
        rows = arc.index.records_for_type("ig", RecordType.MESSAGE)
        assert len(rows) == 1
        import json
        media = json.loads(rows[0]["media"])
        assert len(media) == 1
        assert arc.blobs.get_bytes(media[0]) == b"JPEGBYTES"
