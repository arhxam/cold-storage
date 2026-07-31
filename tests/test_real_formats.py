"""Golden tests built from the REAL 2026 export layouts (verified against public
anonymized samples: kenn-io/msgvault, MasriAm/exodus-opensource, meizuflux/scrollback).

These encode the exact current paths/keys so format drift is caught by CI, not by
the user's real data.
"""

from __future__ import annotations

import json
from pathlib import Path

from saveyourshit.connectors.base import detect_connector
from saveyourshit.connectors.facebook import FacebookConnector
from saveyourshit.connectors.instagram import InstagramConnector
from saveyourshit.models import RecordType


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _collect(connector, root):
    recs, media = [], []
    for b in connector.parse_export(root):
        recs += b.records
        media += b.media
    return recs, media


def _real_instagram(root: Path) -> Path:
    # DMs at the current path, with a sticker (single object, not an array)
    _w(
        root / "your_instagram_activity/messages/inbox/maya_123/message_1.json",
        {
            "participants": [{"name": "Maya"}, {"name": "me"}],
            "title": "Maya",
            "thread_path": "inbox/maya_123",
            "messages": [
                {
                    "sender_name": "me",
                    "timestamp_ms": 1700000000000,
                    "content": "hey",
                    "photos": [
                        {"uri": "your_instagram_activity/messages/inbox/maya_123/photos/p.jpg"}
                    ],
                },
                {"sender_name": "Maya", "timestamp_ms": 1700000100000, "sticker": {"uri": "media/stk.png"}},
            ],
        },
    )
    # IG media uri is rooted at export root (includes your_instagram_activity/ prefix)
    photo = root / "your_instagram_activity/messages/inbox/maya_123/photos/p.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"JPG")
    (root / "media").mkdir(parents=True, exist_ok=True)
    (root / "media/stk.png").write_bytes(b"PNG")
    # followers: bare list, paginated file name
    _w(
        root / "connections/followers_and_following/followers_1.json",
        [{"title": "", "media_list_data": [], "string_list_data": [
            {"href": "https://ig/alice", "value": "alice", "timestamp": 1690000000}]}],
    )
    # following: dict under relationships_following
    _w(
        root / "connections/followers_and_following/following.json",
        {"relationships_following": [{"string_list_data": [
            {"href": "https://ig/bob", "value": "bob", "timestamp": 1690000001}]}]},
    )
    # posts at the CURRENT path your_instagram_activity/media/posts_1.json (bare list)
    _w(
        root / "your_instagram_activity/media/posts_1.json",
        [{"media": [{"uri": "media/posts/202401/x.jpg", "creation_timestamp": 1699000000, "title": "hi"}]}],
    )
    return root


def test_instagram_real_layout(tmp_path):
    root = _real_instagram(tmp_path / "ig")
    assert detect_connector(root).id == "instagram"
    recs, media = _collect(InstagramConnector(), root)
    kinds = {r.type for r in recs}
    assert {RecordType.MESSAGE, RecordType.FOLLOWER, RecordType.FOLLOWING, RecordType.POST} <= kinds
    assert [r.text for r in recs if r.type == RecordType.FOLLOWER] == ["alice"]
    # the sticker (single object) is captured as media
    assert any(m.filename == "stk.png" for m in media)
    # the message photo resolves to a real file
    assert any(m.filename == "p.jpg" and Path(m.source_path).exists() for m in media)


def test_instagram_pre2021_followers_fallback(tmp_path):
    root = tmp_path / "ig2"
    _w(root / "your_instagram_activity/x.json", {"ok": True})  # detect marker
    _w(
        root / "connections/followers_and_following/followers.json",
        {"relationships_followers": [{"string_list_data": [{"value": "carol", "timestamp": 1}]}]},
    )
    recs, _ = _collect(InstagramConnector(), root)
    assert any(r.type == RecordType.FOLLOWER and r.text == "carol" for r in recs)


def _real_facebook(root: Path) -> Path:
    # message media uri is rooted at the OLD layout (messages/...) but the file
    # actually lives under your_facebook_activity/ — the connector must resolve it.
    _w(
        root / "your_facebook_activity/messages/inbox/pal_9/message_1.json",
        {
            "participants": [{"name": "Pal"}, {"name": "me"}],
            "title": "Pal",
            "messages": [
                {
                    "sender_name": "Pal",
                    "timestamp_ms": 1710000000000,
                    "content": "yo",
                    "photos": [{"uri": "messages/inbox/pal_9/photos/q.jpg"}],
                }
            ],
        },
    )
    actual = root / "your_facebook_activity/messages/inbox/pal_9/photos/q.jpg"
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_bytes(b"JPG")
    # friends moved to connections/friends/ in current exports
    _w(root / "connections/friends/your_friends.json", {"friends_v2": [{"name": "Charlie", "timestamp": 1}]})
    # posts at the current long filename
    _w(
        root / "your_facebook_activity/posts/your_posts__check_ins__photos_and_videos_1.json",
        [{"timestamp": 1711000000, "data": [{"post": "hello world"}]}],
    )
    return root


def test_facebook_real_layout(tmp_path):
    root = _real_facebook(tmp_path / "fb")
    assert detect_connector(root).id == "facebook"
    recs, media = _collect(FacebookConnector(), root)
    assert any(r.type == RecordType.MESSAGE and r.text == "yo" for r in recs)
    assert any(r.type == RecordType.FOLLOWER and r.text == "Charlie" for r in recs)
    assert any(r.type == RecordType.POST and r.text == "hello world" for r in recs)
    # the FB message photo resolves despite the uri being rooted at the old layout
    assert any(m.filename == "q.jpg" and Path(m.source_path).exists() for m in media)


def test_facebook_does_not_misdetect_instagram(tmp_path):
    """The FB detector must NOT fire on an Instagram export (the old bug)."""
    ig = _real_instagram(tmp_path / "ig")
    assert FacebookConnector().detect(ig) is False
    assert detect_connector(ig).id == "instagram"
