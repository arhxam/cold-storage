"""Tests for the Discord connector against real package layouts.

Covers the December-2025 rename of ``messages/`` to ``Messages/`` (capital M),
optional ``c`` prefix on channel folders, null entries in index.json, numeric
message IDs, and the legacy messages.csv fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

from saveyourshit.connectors.discord import DiscordConnector
from saveyourshit.models import RecordType

CHANNEL_A = "1234567890123456789"  # will get the "c" prefix
CHANNEL_B = "9876543210987654321"  # bare folder, no prefix
CHANNEL_DELETED = "1111222233334444555"  # null in index.json, no folder


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_package(root: Path, messages_folder: str) -> Path:
    """Build a Discord data package under ``root / messages_folder``."""
    msgs = root / messages_folder
    _write_json(
        msgs / "index.json",
        {
            CHANNEL_A: "Direct Message with pal#1234",
            CHANNEL_B: "general",
            CHANNEL_DELETED: None,  # deleted channel: null name
        },
    )
    # Current-style folder: "c" prefix, messages.json, numeric ID.
    _write_json(msgs / f"c{CHANNEL_A}" / "channel.json", {"id": CHANNEL_A})
    _write_json(
        msgs / f"c{CHANNEL_A}" / "messages.json",
        [
            {
                "ID": 900100200300400500,  # JSON number, must coerce to str
                "Timestamp": "2026-01-05 12:00:00",
                "Contents": "hello from 2026",
                "Attachments": "https://cdn.discordapp.com/attachments/a/b/pic.png",
            },
            {
                "ID": "900100200300400501",
                "Timestamp": "2026-01-05 12:01:00.123000+00:00",
                "Contents": "second",
                "Attachments": "",
            },
        ],
    )
    # Older-style folder: bare channel id, no "c" prefix.
    _write_json(msgs / CHANNEL_B / "channel.json", {"id": CHANNEL_B})
    _write_json(
        msgs / CHANNEL_B / "messages.json",
        [
            {
                "ID": "800100200300400500",
                "Timestamp": "2021-06-01 09:30:00",
                "Contents": "old times",
                "Attachments": "",
            },
        ],
    )
    return root


def _collect(connector, root):
    recs, media = [], []
    for batch in connector.parse_export(root):
        recs += batch.records
        media += batch.media
    return recs, media


def test_detect_capital_m_messages(tmp_path):
    """Post-Dec-2025 packages use Messages/ (capital M) and must be detected."""
    root = _build_package(tmp_path / "package", "Messages")
    assert DiscordConnector().detect(root)


def test_detect_lowercase_messages(tmp_path):
    root = _build_package(tmp_path / "package", "messages")
    assert DiscordConnector().detect(root)


def test_detect_rejects_unrelated_tree(tmp_path):
    root = tmp_path / "not_discord"
    _write_json(root / "data" / "something.json", {"hi": 1})
    assert not DiscordConnector().detect(root)


def test_parse_capital_m_package(tmp_path):
    root = _build_package(tmp_path / "package", "Messages")
    recs, media = _collect(DiscordConnector(), root)

    assert len(recs) == 3
    assert all(r.type is RecordType.MESSAGE for r in recs)
    by_uid = {r.uid: r for r in recs}

    # Numeric ID coerced to string in the uid.
    uid = f"msg:{CHANNEL_A}:900100200300400500"
    assert uid in by_uid
    rec = by_uid[uid]
    assert rec.text == "hello from 2026"
    assert rec.thread == "Direct Message with pal#1234"
    assert rec.created_at == "2026-01-05 12:00:00"  # passed through as-is
    assert rec.extra == {"channel_id": CHANNEL_A}

    # Bare (no "c" prefix) channel folder is parsed too.
    old_uid = f"msg:{CHANNEL_B}:800100200300400500"
    assert old_uid in by_uid
    assert by_uid[old_uid].thread == "general"

    # Attachment URL becomes a MediaRef owned by the message.
    assert len(media) == 1
    assert media[0].owner_uid == uid
    assert media[0].source_url == "https://cdn.discordapp.com/attachments/a/b/pic.png"


def test_parse_lowercase_package(tmp_path):
    root = _build_package(tmp_path / "package", "messages")
    recs, _ = _collect(DiscordConnector(), root)
    assert len(recs) == 3
    assert {r.uid for r in recs} == {
        f"msg:{CHANNEL_A}:900100200300400500",
        f"msg:{CHANNEL_A}:900100200300400501",
        f"msg:{CHANNEL_B}:800100200300400500",
    }


def test_null_index_entry_skipped_and_missing_name_falls_back(tmp_path):
    """Null names (deleted channels) are ignored; a folder for a channel that
    is null in index.json falls back to the channel id as thread name."""
    msgs = tmp_path / "package" / "Messages"
    _write_json(msgs / "index.json", {CHANNEL_DELETED: None})
    _write_json(
        msgs / f"c{CHANNEL_DELETED}" / "messages.json",
        [
            {
                "ID": "700",
                "Timestamp": "2025-12-31 23:59:59",
                "Contents": "ghost channel",
                "Attachments": "",
            }
        ],
    )
    recs, _ = _collect(DiscordConnector(), tmp_path / "package")
    assert len(recs) == 1
    assert recs[0].thread == CHANNEL_DELETED  # fell back to id, not None
    assert recs[0].text == "ghost channel"


def test_csv_fallback(tmp_path):
    """Legacy packages ship messages.csv with the same capitalized headers."""
    msgs = tmp_path / "package" / "messages"
    _write_json(msgs / "index.json", {CHANNEL_A: "csv channel"})
    chan = msgs / f"c{CHANNEL_A}"
    chan.mkdir(parents=True)
    chan.joinpath("messages.csv").write_text(
        "ID,Timestamp,Contents,Attachments\n"
        "600100200300400500,2020-05-01 10:00:00,csv says hi,"
        "https://cdn.discordapp.com/attachments/x/y/z.jpg\n",
        encoding="utf-8",
    )
    recs, media = _collect(DiscordConnector(), tmp_path / "package")
    assert len(recs) == 1
    assert recs[0].uid == f"msg:{CHANNEL_A}:600100200300400500"
    assert recs[0].text == "csv says hi"
    assert recs[0].thread == "csv channel"
    assert len(media) == 1
    assert media[0].source_url == "https://cdn.discordapp.com/attachments/x/y/z.jpg"


def test_nested_package_root(tmp_path):
    """Detection and parsing work when the package sits inside a wrapper dir
    (e.g. an unzipped 'package/' folder)."""
    root = tmp_path / "downloads"
    _build_package(root / "package", "Messages")
    c = DiscordConnector()
    assert c.detect(root)
    recs, _ = _collect(c, root)
    assert len(recs) == 3
