import zipfile

from saveyourshit.crypto import KeyManager
from saveyourshit.engine import Engine
from saveyourshit.models import RecordType
from saveyourshit.store.archive import Archive


def test_ingest_instagram_end_to_end(layout, instagram_export):
    with Archive(layout) as arc:
        result = Engine(arc).ingest(instagram_export)
        assert result.status == "ok"
        assert result.connector == "instagram"
        assert result.added > 0
        # snapshot of the raw export was kept immutably
        assert result.snapshot is not None and result.snapshot.exists()
        # searchable
        assert arc.index.search("café")
        # run recorded for the dead-man's-switch
        assert arc.index.last_run("instagram")["status"] == "ok"


def test_ingest_is_idempotent(layout, instagram_export):
    with Archive(layout) as arc:
        eng = Engine(arc)
        first = eng.ingest(instagram_export)
        count_after_first = arc.index.count("instagram")
        second = eng.ingest(instagram_export)
        assert second.added == 0
        assert arc.index.count("instagram") == count_after_first
        assert first.added == count_after_first


def test_ingest_from_zip(layout, instagram_export, tmp_path):
    zip_path = tmp_path / "ig.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in instagram_export.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(instagram_export))
    with Archive(layout) as arc:
        result = Engine(arc).ingest(zip_path)
        assert result.status == "ok"
        assert result.added > 0


def test_ingest_encrypted(layout, instagram_export):
    km = KeyManager(layout.keys_dir)
    cipher, _ = km.create("pw")
    with Archive(layout, cipher=cipher) as arc:
        result = Engine(arc).ingest(instagram_export)
        assert result.added > 0
        # media blobs are encrypted on disk but verify + decrypt
        rows = arc.index.records_for_type("instagram", RecordType.MESSAGE)
        import json
        for row in rows:
            for sha in json.loads(row["media"]):
                assert arc.blobs.verify(sha)


def test_ingest_folder_scans_multiple_exports(layout, instagram_export, discord_export, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    # move both exports + one junk folder into the "Downloads" dir
    import shutil

    shutil.copytree(instagram_export, downloads / "instagram-data")
    shutil.copytree(discord_export, downloads / "discord-pkg")
    (downloads / "unrelated").mkdir()
    (downloads / "unrelated" / "notes.txt").write_text("nothing")

    with Archive(layout) as arc:
        results = Engine(arc).ingest_folder(downloads)
        connectors = {r.connector for r in results}
        assert {"instagram", "discord"} <= connectors
        assert arc.index.count("instagram") > 0
        assert arc.index.count("discord") > 0


def test_no_snapshot_skips_raw_copy(layout, instagram_export):
    with Archive(layout) as arc:
        result = Engine(arc).ingest(instagram_export, keep_snapshot=False)
        assert result.added > 0
        assert result.snapshot is None
        # no snapshots directory was created
        assert not layout.snapshots_dir("instagram").exists()


def test_ingest_never_modifies_the_source_export(layout, instagram_export):
    import hashlib

    def digest(root):
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    before = digest(instagram_export)
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
    after = digest(instagram_export)
    assert before == after  # the user's original export is byte-for-byte untouched


def test_ingest_folder_on_single_export(layout, instagram_export):
    with Archive(layout) as arc:
        results = Engine(arc).ingest_folder(instagram_export)
        assert len(results) == 1
        assert results[0].connector == "instagram"


def test_unrecognized_export_raises(layout, tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "random.txt").write_text("nothing here")
    with Archive(layout) as arc:
        import pytest
        with pytest.raises(ValueError):
            Engine(arc).ingest(junk)
