"""Tests for the Google Takeout connector (self-contained fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

from saveyourshit.connectors.google import GoogleConnector
from saveyourshit.models import RecordType

WATCH_HISTORY = [
    {
        "header": "YouTube",
        "title": "Watched How to Solder Anything",
        "titleUrl": "https://www.youtube.com/watch?v=abc123",
        "subtitles": [
            {"name": "Workshop Channel", "url": "https://www.youtube.com/channel/UCxyz"}
        ],
        "time": "2024-03-15T18:23:45.123Z",
        "products": ["YouTube"],
    },
    {
        # Deleted/private video: no subtitles key at all.
        "header": "YouTube",
        "title": "Watched a video that has been removed",
        "time": "2024-03-14T09:00:00Z",
    },
]

SEARCH_HISTORY = [
    {
        "header": "YouTube",
        "title": "Searched for soldering iron review",
        "titleUrl": "https://www.youtube.com/results?search_query=soldering+iron+review",
        "time": "2024-03-15T18:20:00.000Z",
    }
]

SUBSCRIPTIONS_CSV = (
    "Channel Id,Channel Url,Channel Title\n"
    "UCabc,https://www.youtube.com/channel/UCabc,Workshop Channel\n"
    "UCdef,https://www.youtube.com/channel/UCdef,Cooking Daily\n"
)

CHAT_MESSAGES = {
    "messages": [
        {
            "creator": {"name": "Ada Lovelace", "email": "ada@example.com", "user_type": "Human"},
            "created_date": "Tuesday, January 2, 2024 at 10:15:00 AM UTC",
            "text": "hey, did you see the export?",
            "topic_id": "t1",
        },
        {
            "creator": {"name": "Grace Hopper", "email": "grace@example.com"},
            "created_date": "Tuesday, January 2, 2024 at 10:16:30 AM UTC",
            "text": "yep, parsing it now",
        },
    ]
}


def make_takeout(tmp_path: Path) -> Path:
    root = tmp_path / "Takeout"
    yt = root / "YouTube and YouTube Music"
    history = yt / "history"
    history.mkdir(parents=True)
    (history / "watch-history.json").write_text(json.dumps(WATCH_HISTORY), encoding="utf-8")
    (history / "search-history.json").write_text(json.dumps(SEARCH_HISTORY), encoding="utf-8")

    subs = yt / "subscriptions"
    subs.mkdir()
    (subs / "subscriptions.csv").write_text(SUBSCRIPTIONS_CSV, encoding="utf-8")

    group = root / "Google Chat" / "Groups" / "DM 4on2rE4AAAE"
    group.mkdir(parents=True)
    (group / "messages.json").write_text(json.dumps(CHAT_MESSAGES), encoding="utf-8")

    (root / "archive_browser.html").write_text("<html></html>", encoding="utf-8")
    return root


def parse_all(root: Path):
    connector = GoogleConnector()
    records = []
    for batch in connector.parse_export(root):
        records.extend(batch.records)
    return records


# -- detect ------------------------------------------------------------------


def test_detect_full_takeout(tmp_path: Path):
    root = make_takeout(tmp_path)
    assert GoogleConnector().detect(root)
    # Also detects when pointed one level up (the unpacked zip dir).
    assert GoogleConnector().detect(tmp_path)


def test_detect_via_archive_browser_marker_only(tmp_path: Path):
    (tmp_path / "archive_browser.html").write_text("<html></html>", encoding="utf-8")
    assert GoogleConnector().detect(tmp_path)


def test_detect_via_subscriptions_csv_only(tmp_path: Path):
    subs = tmp_path / "some" / "subscriptions"
    subs.mkdir(parents=True)
    (subs / "subscriptions.csv").write_text(SUBSCRIPTIONS_CSV, encoding="utf-8")
    assert GoogleConnector().detect(tmp_path)


def test_detect_rejects_unrelated_dir(tmp_path: Path):
    (tmp_path / "random.txt").write_text("nope", encoding="utf-8")
    assert not GoogleConnector().detect(tmp_path)


def test_detect_rejects_csv_with_wrong_columns(tmp_path: Path):
    subs = tmp_path / "subscriptions"
    subs.mkdir()
    (subs / "subscriptions.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert not GoogleConnector().detect(tmp_path)


# -- watch / search history --------------------------------------------------


def test_parse_watch_history(tmp_path: Path):
    records = parse_all(make_takeout(tmp_path))
    watches = [r for r in records if r.extra.get("kind") == "youtube_watch"]
    assert len(watches) == 2

    first = watches[0]
    assert first.type == RecordType.OTHER
    assert first.text == "Watched How to Solder Anything"
    assert first.author == "Workshop Channel"
    assert first.created_at == "2024-03-15T18:23:45.123Z"
    assert first.extra["url"] == "https://www.youtube.com/watch?v=abc123"
    assert first.connector == "google"

    # Entry without subtitles has no author but still parses.
    assert watches[1].author is None
    assert watches[1].text == "Watched a video that has been removed"


def test_parse_search_history(tmp_path: Path):
    records = parse_all(make_takeout(tmp_path))
    searches = [r for r in records if r.extra.get("kind") == "youtube_search"]
    assert len(searches) == 1
    assert searches[0].text == "Searched for soldering iron review"
    assert searches[0].created_at == "2024-03-15T18:20:00.000Z"


# -- subscriptions -----------------------------------------------------------


def test_parse_subscriptions(tmp_path: Path):
    records = parse_all(make_takeout(tmp_path))
    subs = [r for r in records if r.type == RecordType.FOLLOWING]
    assert len(subs) == 2
    by_uid = {r.uid: r for r in subs}
    assert by_uid["yt:sub:UCabc"].text == "Workshop Channel"
    assert by_uid["yt:sub:UCdef"].extra["channel_url"] == "https://www.youtube.com/channel/UCdef"


# -- Google Chat -------------------------------------------------------------


def test_parse_google_chat(tmp_path: Path):
    records = parse_all(make_takeout(tmp_path))
    msgs = [r for r in records if r.type == RecordType.MESSAGE]
    assert len(msgs) == 2

    first = msgs[0]
    assert first.author == "Ada Lovelace"
    assert first.text == "hey, did you see the export?"
    assert first.thread == "DM 4on2rE4AAAE"
    assert first.created_at == "2024-01-02T10:15:00+00:00"
    assert first.extra["email"] == "ada@example.com"


def test_chat_unparseable_date_falls_back_to_none(tmp_path: Path):
    group = tmp_path / "Google Chat" / "Groups" / "Space AAAA"
    group.mkdir(parents=True)
    payload = {
        "messages": [
            {
                "creator": {"name": "X"},
                "created_date": "not a date",
                "text": "hi",
            }
        ]
    }
    (group / "messages.json").write_text(json.dumps(payload), encoding="utf-8")
    records = parse_all(tmp_path)
    assert len(records) == 1
    assert records[0].created_at is None
    assert records[0].extra["created_date_raw"] == "not a date"


# -- general -----------------------------------------------------------------


def test_uids_unique_and_stable(tmp_path: Path):
    root = make_takeout(tmp_path)
    uids1 = [r.uid for r in parse_all(root)]
    uids2 = [r.uid for r in parse_all(root)]
    assert len(uids1) == len(set(uids1))  # unique within the connector
    assert uids1 == uids2  # stable across re-parses


def test_malformed_files_are_skipped(tmp_path: Path):
    root = make_takeout(tmp_path)
    history = root / "YouTube and YouTube Music" / "history"
    (history / "watch-history.json").write_text("{not json", encoding="utf-8")
    records = parse_all(root)  # must not raise
    assert all(r.extra.get("kind") != "youtube_watch" for r in records)
    assert any(r.type == RecordType.MESSAGE for r in records)
