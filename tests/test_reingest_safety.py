"""Re-ingesting an updated export must not lose or duplicate anything.

Every platform here delivers updates by handing back the *whole* export with
new items added — usually at the top. An audit found several connectors keyed
records by list position, which meant a single new item renamed everything
after it: the archive stored a second copy of the old data, and the genuinely
new item collided with an existing uid and was dropped. The user was told it
worked. These tests pin the fix.
"""

import json
import zipfile
from pathlib import Path

from coldstorage.connectors import get as get_connector


def _uids(connector_id: str, root: Path) -> list[str]:
    return [r.uid for b in get_connector(connector_id).parse_export(root) for r in b.records]


# --- Instagram posts ------------------------------------------------------


def _ig_posts(root: Path, titles: list[str]) -> Path:
    d = root / "your_instagram_activity" / "content"
    d.mkdir(parents=True, exist_ok=True)
    (root / "connections" / "followers_and_following").mkdir(parents=True, exist_ok=True)
    stamps = {"old a": 1700000001, "old b": 1700000002, "BRAND NEW": 1700009999,
              "one": 1700000010, "two": 1700000020, "three": 1700000030}
    (d / "posts_1.json").write_text(
        json.dumps(
            [
                {"media": [{"uri": f"media/{t}.jpg", "creation_timestamp": stamps[t]}],
                 "title": t}
                for t in titles
            ]
        )
    )
    return root


def test_instagram_new_post_does_not_duplicate_the_old_ones(tmp_path):
    before = _uids("instagram", _ig_posts(tmp_path / "v1", ["old a", "old b"]))
    # Instagram prepends: the new post arrives on top.
    after = _uids("instagram", _ig_posts(tmp_path / "v2", ["BRAND NEW", "old a", "old b"]))

    assert len(before) == 2 and len(after) == 3
    assert set(before) < set(after), "every original post keeps its identity"
    assert len(set(after)) == 3, "no post is stored twice"


def test_instagram_identical_reexport_is_a_no_op(tmp_path):
    a = _uids("instagram", _ig_posts(tmp_path / "a", ["one", "two", "three"]))
    b = _uids("instagram", _ig_posts(tmp_path / "b", ["one", "two", "three"]))
    assert a == b


# --- Snapchat chats -------------------------------------------------------


def _snap(root: Path, msgs: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamps = {"older A": "2025-01-01 10:00:00 UTC", "older B": "2025-01-02 10:00:00 UTC",
              "NEW MESSAGE": "2025-01-09 10:00:00 UTC"}
    (root / "chat_history.json").write_text(
        json.dumps(
            {
                "alice": [
                    {"From": "alice", "Created": stamps[m], "Content": m} for m in msgs
                ]
            }
        )
    )
    return root


def test_snapchat_new_message_is_not_lost(tmp_path):
    before = _uids("snapchat", _snap(tmp_path / "v1", ["older A", "older B"]))
    after_root = _snap(tmp_path / "v2", ["NEW MESSAGE", "older A", "older B"])
    after = _uids("snapchat", after_root)

    assert len(set(after)) == 3, "three distinct messages"
    assert set(before) < set(after), "the old ones keep their identity"
    texts = [
        r.text
        for b in get_connector("snapchat").parse_export(after_root)
        for r in b.records
    ]
    assert "NEW MESSAGE" in texts, "the newest message must actually be stored"


# --- WhatsApp: two chats must not collide --------------------------------


CHAT = "[05/01/2025, 10:00:00] {who}: {msg}\n"


def _wa_zip(path: Path, who: str, msg: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_chat.txt", CHAT.format(who=who, msg=msg))
    return path


def test_two_whatsapp_chat_zips_do_not_collide(tmp_path):
    """Both iOS zips contain only "_chat.txt", so the folder name is the chat."""
    from coldstorage.engine import Engine
    from coldstorage.paths import Layout
    from coldstorage.store.archive import Archive

    alice = _wa_zip(tmp_path / "WhatsApp Chat with Alice.zip", "Alice", "hello from alice")
    bob = _wa_zip(tmp_path / "WhatsApp Chat with Bob.zip", "Bob", "hello from bob")

    layout = Layout(tmp_path / "home")
    layout.ensure()
    with Archive(layout) as arc:
        engine = Engine(arc)
        r1 = engine.ingest(alice, keep_snapshot=False)
        r2 = engine.ingest(bob, keep_snapshot=False)
        assert r1.added == 1
        assert r2.added == 1, "Bob's chat must not be swallowed as a duplicate of Alice's"
        threads = {t["thread"] for t in arc.index.threads("whatsapp")}
        texts = {r["text"] for r in arc.index.iter_records()}
    assert "hello from alice" in texts and "hello from bob" in texts
    assert len(threads) == 2, f"expected two separate conversations, got {threads}"


# --- Meta: several photos sent at once are distinct messages --------------


def test_meta_photos_sent_together_stay_separate_messages(tmp_path):
    d = tmp_path / "your_instagram_activity" / "messages" / "inbox" / "maya"
    d.mkdir(parents=True)
    (tmp_path / "connections" / "followers_and_following").mkdir(parents=True)
    (d / "message_1.json").write_text(
        json.dumps(
            {
                "title": "Maya",
                "messages": [
                    # Same second, no text — only the attachment differs.
                    {"sender_name": "Me", "timestamp_ms": 1700000000000,
                     "photos": [{"uri": f"media/p{i}.jpg"}]}
                    for i in range(3)
                ],
            }
        )
    )
    records = [r for b in get_connector("instagram").parse_export(tmp_path) for r in b.records]
    msgs = [r for r in records if r.type.value == "message"]
    assert len(msgs) == 3, "three photos sent at once are three messages, not one"
    assert len({m.uid for m in msgs}) == 3
