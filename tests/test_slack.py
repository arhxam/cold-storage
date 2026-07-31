"""Tests for the Slack export connector (self-contained fixtures in tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

from saveyourshit.connectors.slack import SlackConnector


def make_export(root: Path) -> Path:
    """Build a minimal Slack workspace export under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "users.json").write_text(
        json.dumps(
            [
                {
                    "id": "U111",
                    "name": "alice",
                    "real_name": "Alice Anderson",
                    "profile": {"display_name": "alice.a"},
                },
                {
                    "id": "U222",
                    "name": "bob",
                    "real_name": "Bob Brown",
                    "profile": {"display_name": ""},
                },
            ]
        ),
        encoding="utf-8",
    )
    (root / "channels.json").write_text(
        json.dumps([{"id": "C001", "name": "general"}]), encoding="utf-8"
    )
    general = root / "general"
    general.mkdir()
    (general / "2020-09-13.json").write_text(
        json.dumps(
            [
                {
                    "type": "message",
                    "user": "U111",
                    "text": "hello <@U222> see <http://example.com|this>",
                    "ts": "1600000000.000200",
                },
                {
                    "type": "message",
                    "user": "U222",
                    "text": "reply from bob",
                    "ts": "1600000060.000300",
                },
                {
                    "type": "message",
                    "user": "U999",
                    "text": "from an unknown user",
                    "ts": "1600000120.000400",
                    "files": [
                        {
                            "name": "report.pdf",
                            "url_private": "https://files.slack.com/report.pdf",
                        }
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_detect_positive_and_negative(tmp_path: Path) -> None:
    conn = SlackConnector()
    export = make_export(tmp_path / "export")
    assert conn.detect(export)
    # Also detected when the export root is nested one level down.
    assert conn.detect(tmp_path)

    empty = tmp_path / "empty"
    empty.mkdir()
    assert not conn.detect(empty)

    # channels.json alone is not enough.
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "channels.json").write_text("[]", encoding="utf-8")
    assert not conn.detect(partial)


def test_parse_messages_and_user_resolution(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export")
    batches = list(SlackConnector().parse_export(export))
    assert len(batches) == 1
    records = batches[0].records
    assert len(records) == 3

    by_uid = {r.uid: r for r in records}
    first = by_uid["msg:general:1600000000.000200"]
    # display_name wins when present.
    assert first.author == "alice.a"
    assert first.thread == "general"
    assert first.type == "message"
    # Slack markup is left as-is.
    assert first.text == "hello <@U222> see <http://example.com|this>"

    # Empty display_name falls back to real_name.
    assert by_uid["msg:general:1600000060.000300"].author == "Bob Brown"
    # Unknown user id falls back to the raw id.
    assert by_uid["msg:general:1600000120.000400"].author == "U999"


def test_ts_converted_to_iso_utc(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export")
    batches = list(SlackConnector().parse_export(export))
    first = batches[0].records[0]
    # 1600000000.000200 -> 2020-09-13T12:26:40.000200+00:00 UTC
    assert first.created_at == "2020-09-13T12:26:40.000200+00:00"
    assert first.created_at.endswith("+00:00")


def test_file_attachments_become_media_refs(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export")
    batches = list(SlackConnector().parse_export(export))
    media = batches[0].media
    assert len(media) == 1
    ref = media[0]
    assert ref.owner_uid == "msg:general:1600000120.000400"
    assert ref.kind == "file"
    assert ref.source_url == "https://files.slack.com/report.pdf"
    assert ref.filename == "report.pdf"
