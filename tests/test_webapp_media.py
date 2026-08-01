"""The /media/ route: serving the user's own photos back to them.

Blobs are addressed by content hash and stored encrypted. This route is the
only way the UI can show them, so it has to be both correct and unwilling to
serve anything that isn't a blob.
"""

import json

import pytest

from coldstorage.config import Config
from coldstorage.store.archive import Archive
from coldstorage.webapp import _sniff_media_type, handle

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _get(layout, path, qs=None):
    with Archive(layout) as arc:
        return handle(path, qs or {}, arc, Config())


def test_serves_a_stored_photo_with_the_right_type(layout):
    with Archive(layout) as arc:
        sha = arc.blobs.put_bytes(PNG)
    code, ctype, body = _get(layout, f"/media/{sha}")
    assert code == 200
    assert ctype == "image/png"
    assert body == PNG, "bytes come back exactly as stored"


def test_unknown_blob_is_a_clean_404(layout):
    code, _, _ = _get(layout, "/media/" + "a" * 64)
    assert code == 404


@pytest.mark.parametrize(
    "bad",
    [
        "",  # nothing
        "../../etc/passwd",  # traversal
        "a" * 63,  # too short
        "a" * 65,  # too long
        "g" * 64,  # not hex
        "A" * 63 + "/",  # slash smuggled in
    ],
)
def test_refuses_anything_that_is_not_a_content_hash(layout, bad):
    code, _, _ = _get(layout, f"/media/{bad}")
    assert code in (400, 404), f"{bad!r} must not be served"


def test_type_comes_from_the_bytes_not_the_filename():
    # A file inside someone's export could claim any name; the magic number is
    # the only thing that can't lie.
    assert _sniff_media_type(PNG, "evil.html") == "image/png"
    assert _sniff_media_type(b"<script>alert(1)</script>", "photo.png") == "application/octet-stream"
    assert _sniff_media_type(b"\xff\xd8\xff\xe0junk", None) == "image/jpeg"
    assert _sniff_media_type(b"GIF89a...", None) == "image/gif"
    assert _sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ", None) == "image/webp"
    assert _sniff_media_type(b"\x00\x00\x00\x18ftypmp42", None) == "video/mp4"


def test_html_and_svg_are_never_served_as_markup(layout):
    """The whole point of sniffing: a hostile file can't run in our origin."""
    for payload in (b"<html><script>alert(1)</script></html>", b"<svg onload=alert(1)>"):
        with Archive(layout) as arc:
            sha = arc.blobs.put_bytes(payload)
        code, ctype, _ = _get(layout, f"/media/{sha}")
        assert code == 200
        assert ctype == "application/octet-stream"
        assert "html" not in ctype and "svg" not in ctype


def test_media_hashes_reach_the_thread_api(layout):
    """A message's attachments must survive into what the UI reads."""
    from coldstorage.models import NormalizedRecord, RecordType

    with Archive(layout) as arc:
        sha = arc.blobs.put_bytes(PNG)
        arc.index.upsert_many(  # upsert_many is what commits
            [
                NormalizedRecord(
                    connector="instagram",
                    type=RecordType.MESSAGE,
                    uid="1",
                    created_at="2025-01-01T00:00:00+00:00",
                    author="Maya",
                    thread="Maya",
                    text="look at this",
                    media=[sha],
                )
            ]
        )
    code, _, body = _get(
        layout, "/api/thread", {"connector": ["instagram"], "thread": ["Maya"]}
    )
    assert code == 200
    rows = json.loads(body)
    assert rows and json.loads(rows[0]["media"]) == [sha]
