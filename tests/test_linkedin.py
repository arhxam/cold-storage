"""Tests for the LinkedIn export connector (self-contained fixtures in tmp_path)."""

from __future__ import annotations

from pathlib import Path

from saveyourshit.connectors.linkedin import LinkedInConnector
from saveyourshit.models import RecordType

MESSAGES_CSV = (
    "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,DATE,SUBJECT,CONTENT,FOLDER\n"
    "conv-1,,Alice Anderson,https://linkedin.com/in/alice,Me,2023-01-05 10:00:00 UTC,,"
    "Hey! Long time no see,INBOX\n"
    "conv-1,,Me,https://linkedin.com/in/me,Alice Anderson,2023-01-05 10:05:00 UTC,,"
    "\"Hi Alice, indeed!\",INBOX\n"
    "conv-2,,Bob Brown,https://linkedin.com/in/bob,Me,2023-02-01 09:00:00 UTC,"
    "Job opportunity,Are you open to a new role?,INBOX\n"
)

CONNECTIONS_CSV = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the email addresses '
    'are missing."\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Alice,Anderson,https://www.linkedin.com/in/alice,alice@example.com,Acme Corp,"
    "Engineer,05 Jan 2023\n"
    "Bob,Brown,https://www.linkedin.com/in/bob,,Globex,Manager,01 Feb 2023\n"
)


def make_export(root: Path, *, messages: bool = True, connections: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if messages:
        (root / "messages.csv").write_text(MESSAGES_CSV, encoding="utf-8")
    if connections:
        (root / "Connections.csv").write_text(CONNECTIONS_CSV, encoding="utf-8")
    return root


def parse_all(path: Path):
    records = []
    for batch in LinkedInConnector().parse_export(path):
        records.extend(batch.records)
    return records


def test_detect_full_export(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export")
    assert LinkedInConnector().detect(export)


def test_detect_messages_only(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export", connections=False)
    assert LinkedInConnector().detect(export)


def test_detect_connections_only(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export", messages=False)
    assert LinkedInConnector().detect(export)


def test_detect_rejects_unrelated_dir(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "comments.csv").write_text("id,subreddit,body\nabc,python,hi\n", encoding="utf-8")
    assert not LinkedInConnector().detect(other)


def test_parse_messages(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export", connections=False)
    records = parse_all(export)
    assert len(records) == 3
    msgs = [r for r in records if r.type == RecordType.MESSAGE]
    assert len(msgs) == 3

    first = msgs[0]
    assert first.connector == "linkedin"
    assert first.uid == "msg:conv-1:0"
    assert first.author == "Alice Anderson"
    assert first.thread == "conv-1"
    assert first.created_at == "2023-01-05 10:00:00 UTC"
    assert first.text == "Hey! Long time no see"

    # per-conversation index makes uids unique and stable
    assert msgs[1].uid == "msg:conv-1:1"
    assert msgs[2].uid == "msg:conv-2:0"
    assert msgs[2].extra["subject"] == "Job opportunity"


def test_parse_connections_skips_notes_preamble(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export", messages=False)
    records = parse_all(export)
    conns = [r for r in records if r.type == RecordType.FOLLOWER]
    # the "Notes:" preamble lines must not turn into records
    assert len(conns) == 2
    assert all(r.text not in (None, "Notes:") for r in conns)

    alice = next(r for r in conns if r.text == "Alice Anderson")
    assert alice.created_at == "05 Jan 2023"
    assert alice.extra["company"] == "Acme Corp"
    assert alice.extra["position"] == "Engineer"
    assert alice.uid.startswith("conn:")

    bob = next(r for r in conns if r.text == "Bob Brown")
    assert bob.extra["company"] == "Globex"
    assert "email" not in bob.extra  # blank fields stay out of extra


def test_uids_unique_and_stable(tmp_path: Path) -> None:
    export = make_export(tmp_path / "export")
    uids = [r.uid for r in parse_all(export)]
    assert len(uids) == len(set(uids))
    assert uids == [r.uid for r in parse_all(export)]  # re-parse yields same uids


def test_connections_without_preamble_still_parse(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    no_preamble = CONNECTIONS_CSV.split("\n\n", 1)[1]
    assert no_preamble.startswith("First Name")
    (export / "Connections.csv").write_text(no_preamble, encoding="utf-8")
    records = parse_all(export)
    assert len(records) == 2
