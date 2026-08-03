"""Google Takeout can arrive as .tar.gz — ensure_unpacked must handle it safely."""

import io
import json
import tarfile

import pytest

from coldstorage.connectors.base import detect_connector, ensure_unpacked


def test_targz_unpacks_and_detects(tmp_path):
    src = tmp_path / "Takeout" / "YouTube and YouTube Music" / "history"
    src.mkdir(parents=True)
    (src / "watch-history.json").write_text(
        json.dumps([{"title": "X", "titleUrl": "u", "time": "2024-01-01T00:00:00Z",
                     "subtitles": [{"name": "c"}]}])
    )
    tgz = tmp_path / "takeout-20260101.tgz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(tmp_path / "Takeout", arcname="Takeout")

    out = ensure_unpacked(tgz, tmp_path / "dest")
    assert out.is_dir()
    det = detect_connector(out)
    assert det is not None and det.id == "google"


def test_targz_rejects_path_traversal(tmp_path):
    bad = tmp_path / "evil.tgz"
    with tarfile.open(bad, "w:gz") as t:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"pwned"
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError):
        ensure_unpacked(bad, tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()
