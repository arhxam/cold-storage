"""Self-contained tests for the Snapchat "My Data" export connector."""

import json

import pytest

from coldstorage.connectors.snapchat import SnapchatConnector
from coldstorage.models import RecordType


def _write(root, name, payload):
    d = root / "json"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def snapchat_export(tmp_path):
    _write(
        tmp_path,
        "chat_history.json",
        {
            "Received Saved Chats": [
                {
                    "From": "alice",
                    "Media Type": "TEXT",
                    "Created": "2021-01-01 12:00:00 UTC",
                    "Text": "hi",
                    "Conversation Title": "besties",
                }
            ],
            "bob": [
                {
                    "From": "bob",
                    "Media Type": "TEXT",
                    "Created": "2022-06-15 08:30:00 UTC",
                    "Text": "yo",
                },
                {
                    "From": "me",
                    "Media Type": "MEDIA",
                    "Created": "2022-06-15 08:31:00 UTC",
                },
            ],
        },
    )
    _write(
        tmp_path,
        "memories_history.json",
        {
            "Saved Media": [
                {
                    "Date": "2020-05-05 10:00:00 UTC",
                    "Media Type": "Image",
                    "Download Link": "https://app.snapchat.com/dmd/memories?uid=abc123",
                },
                {
                    "Date": "2020-05-06 11:00:00 UTC",
                    "Media Type": "Video",
                    "Download Link": "https://app.snapchat.com/dmd/memories?uid=def456",
                },
            ]
        },
    )
    _write(
        tmp_path,
        "friends.json",
        {
            "Friends": [
                {"Username": "alice", "Display Name": "Alice A"},
                {"Username": "bob", "Display Name": "Bobby"},
            ]
        },
    )
    return tmp_path


def _collect(root):
    recs, media = [], []
    for batch in SnapchatConnector().parse_export(root):
        recs += batch.records
        media += batch.media
    return recs, media


def test_detect(snapchat_export, tmp_path):
    c = SnapchatConnector()
    assert c.detect(snapchat_export)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not c.detect(empty)


def test_detect_chat_only(tmp_path):
    _write(tmp_path, "chat_history.json", {"alice": []})
    assert SnapchatConnector().detect(tmp_path)


def test_detect_nested_json_dir(tmp_path):
    nested = tmp_path / "mydata~1234"
    _write(nested, "memories_history.json", {"Saved Media": []})
    assert SnapchatConnector().detect(tmp_path)


def test_chat_parsing(snapchat_export):
    recs, _ = _collect(snapchat_export)
    msgs = [r for r in recs if r.type == RecordType.MESSAGE]
    assert len(msgs) == 3
    by_text = {r.text: r for r in msgs}
    assert by_text["hi"].author == "alice"
    # "Conversation Title" wins over the bucket key
    assert by_text["hi"].thread == "besties"
    assert by_text["hi"].created_at == "2021-01-01T12:00:00+00:00"
    # partner-keyed shape: thread falls back to the top-level key
    assert by_text["yo"].thread == "bob"
    assert by_text["yo"].author == "bob"
    # message with no Text still yields a record
    assert any(r.text is None and r.author == "me" for r in msgs)
    # uids unique
    assert len({r.uid for r in msgs}) == 3
    assert all(r.connector == "snapchat" for r in msgs)


def test_memories_parsing(snapchat_export):
    recs, media = _collect(snapchat_export)
    mems = [r for r in recs if r.type == RecordType.MEDIA]
    assert len(mems) == 2
    assert mems[0].created_at == "2020-05-05T10:00:00+00:00"
    assert len(media) == 2
    by_kind = {m.kind: m for m in media}
    assert by_kind["image"].source_url == "https://app.snapchat.com/dmd/memories?uid=abc123"
    assert by_kind["video"].source_url == "https://app.snapchat.com/dmd/memories?uid=def456"
    assert {m.owner_uid for m in media} == {r.uid for r in mems}
    assert all(m.source_path is None for m in media)


def test_friends_parsing(snapchat_export):
    recs, _ = _collect(snapchat_export)
    friends = [r for r in recs if r.type == RecordType.FOLLOWER]
    assert [r.text for r in friends] == ["alice", "bob"]
    assert friends[0].extra["display_name"] == "Alice A"


def test_friends_optional(tmp_path):
    _write(
        tmp_path,
        "chat_history.json",
        {"alice": [{"From": "alice", "Created": "2021-01-01 12:00:00 UTC", "Text": "hi"}]},
    )
    recs, _ = _collect(tmp_path)
    assert len(recs) == 1
    assert recs[0].type == RecordType.MESSAGE


def test_malformed_chat_shapes_ignored(tmp_path):
    _write(
        tmp_path,
        "chat_history.json",
        {"not a list": "nope", "alice": ["not a dict", {"From": "alice", "Text": "ok"}]},
    )
    recs, _ = _collect(tmp_path)
    assert [r.text for r in recs] == ["ok"]
    assert recs[0].created_at is None
