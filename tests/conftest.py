"""Shared fixtures: a temp home and builders for realistic platform exports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coldstorage.paths import Layout


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    """Use a cheap scrypt cost in tests, and never touch the real OS keychain."""
    monkeypatch.setenv("COLD_SCRYPT_N", "1024")
    monkeypatch.setenv("COLD_NO_KEYRING", "1")
    monkeypatch.setenv("COLUMNS", "200")  # keep Rich from wrapping long paths in captured output


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "ColdStorage"
    monkeypatch.setenv("COLD_HOME", str(h))
    return h


@pytest.fixture
def layout(home) -> Layout:
    return Layout(home).ensure()


# -- export builders --------------------------------------------------------

def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def instagram_export(tmp_path) -> Path:
    """A minimal but structurally-real Instagram export directory."""
    root = tmp_path / "ig_export"
    # DM thread (note the mojibake in content + name: "Beyoncé" garbled)
    write_json(
        root / "your_instagram_activity/messages/inbox/beyonce_123/message_1.json",
        {
            "participants": [{"name": "BeyoncÃ©"}, {"name": "me"}],
            "title": "BeyoncÃ©",
            "thread_path": "inbox/beyonce_123",
            "messages": [
                {
                    "sender_name": "me",
                    "timestamp_ms": 1600000000000,
                    "content": "cafÃ© tomorrow?",
                },
                {
                    "sender_name": "BeyoncÃ©",
                    "timestamp_ms": 1600000100000,
                    "content": "yes!",
                    "photos": [{"uri": "media/photo1.jpg"}],
                },
            ],
        },
    )
    # a referenced media file
    (root / "media").mkdir(parents=True, exist_ok=True)
    (root / "media/photo1.jpg").write_bytes(b"\xff\xd8\xff\xe0JPEGDATA")
    # followers
    write_json(
        root / "connections/followers_and_following/followers_1.json",
        [{"string_list_data": [{"value": "alice", "href": "https://ig/alice", "timestamp": 1590000000}]}],
    )
    # following
    write_json(
        root / "connections/followers_and_following/following.json",
        {"relationships_following": [
            {"string_list_data": [{"value": "bob", "href": "https://ig/bob", "timestamp": 1590000001}]}
        ]},
    )
    # posts
    write_json(
        root / "content/posts_1.json",
        [{"media": [{"uri": "media/photo1.jpg", "creation_timestamp": 1595000000, "title": "sunset"}]}],
    )
    return root


@pytest.fixture
def facebook_export(tmp_path) -> Path:
    root = tmp_path / "fb_export"
    write_json(
        root / "your_facebook_activity/messages/inbox/pal_9/message_1.json",
        {
            "participants": [{"name": "Pal"}, {"name": "me"}],
            "title": "Pal",
            "messages": [
                {"sender_name": "Pal", "timestamp_ms": 1610000000000, "content": "hey"},
            ],
        },
    )
    write_json(
        root / "friends_and_followers/your_friends.json",
        {"friends_v2": [{"name": "Charlie", "timestamp": 1500000000}]},
    )
    write_json(
        root / "your_facebook_activity/posts/your_posts__1.json",
        [{"timestamp": 1611000000, "data": [{"post": "hello world"}]}],
    )
    return root


@pytest.fixture
def discord_export(tmp_path) -> Path:
    root = tmp_path / "discord_export"
    write_json(root / "messages/index.json", {"111": "Direct Message with pal"})
    write_json(
        root / "messages/c111/messages.json",
        [
            {"ID": "900", "Timestamp": "2023-01-01T00:00:00", "Contents": "yo", "Attachments": ""},
            {"ID": "901", "Timestamp": "2023-01-01T00:01:00", "Contents": "sup", "Attachments": ""},
        ],
    )
    return root


@pytest.fixture
def twitter_export(tmp_path) -> Path:
    root = tmp_path / "twitter_export"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tweets.js").write_text(
        'window.YTD.tweets.part0 = '
        + json.dumps([{"tweet": {"id_str": "42", "created_at": "Wed Oct 10 20:19:24 +0000 2018", "full_text": "gm"}}]),
        encoding="utf-8",
    )
    (data_dir / "direct-messages.js").write_text(
        'window.YTD.direct_messages.part0 = '
        + json.dumps([
            {"dmConversation": {"conversationId": "c1", "messages": [
                {"messageCreate": {"id": "7", "text": "hi there", "senderId": "u1", "createdAt": "2020-01-01T00:00:00.000Z"}}
            ]}}
        ]),
        encoding="utf-8",
    )
    (data_dir / "follower.js").write_text(
        'window.YTD.follower.part0 = ' + json.dumps([{"follower": {"accountId": "555"}}]),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def telegram_export(tmp_path) -> Path:
    root = tmp_path / "tg_export"
    write_json(
        root / "result.json",
        {
            "about": "…",
            "chats": {"list": [
                {
                    "name": "Pal",
                    "type": "personal_chat",
                    "id": 42,
                    "messages": [
                        {"id": 1, "type": "message", "date": "2021-01-01T12:00:00",
                         "from": "Me", "from_id": "user1", "text": "hi there"},
                        {"id": 2, "type": "message", "date": "2021-01-01T12:01:00",
                         "from": "Pal", "from_id": "user2",
                         "text": ["check ", {"type": "bold", "text": "this"}, " out"]},
                        {"id": 3, "type": "service", "date": "2021-01-01T12:02:00"},
                    ],
                }
            ]},
        },
    )
    return root


@pytest.fixture
def reddit_export(tmp_path) -> Path:
    root = tmp_path / "reddit_export"
    root.mkdir(parents=True, exist_ok=True)
    (root / "comments.csv").write_text(
        "id,permalink,date,subreddit,body\n"
        "c1,/r/python/x,2021-01-01,python,great post\n",
        encoding="utf-8",
    )
    (root / "posts.csv").write_text(
        "id,permalink,date,subreddit,title,body\n"
        "p1,/r/python/y,2021-01-02,python,My title,my body\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def whatsapp_export(tmp_path) -> Path:
    root = tmp_path / "wa_export"
    root.mkdir(parents=True, exist_ok=True)
    (root / "WhatsApp Chat with Alice.txt").write_text(
        "[2021-01-01, 12:00:00] Alice: Hello there\n"
        "[2021-01-01, 12:00:05] Me: hi!\n"
        "this is a second line\n"
        "[2021-01-01, 12:01:00] Alice: IMG-0001.jpg (file attached)\n",
        encoding="utf-8",
    )
    (root / "IMG-0001.jpg").write_bytes(b"JPEGDATA")
    return root
