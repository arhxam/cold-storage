"""Real-world WhatsApp "Export chat" format tests.

Fixtures mirror actual export files observed in the wild (gesiscss/WhatsR,
Pustur/whatsapp-chat-parser, starkdmi/whats_json, KnugiHK/WhatsApp-Chat-Exporter):
iOS bracketed 24h and AM/PM formats with LRM/BOM marks, Android dash format,
and EU day-first dotted dates.
"""

from __future__ import annotations

from pathlib import Path

from saveyourshit.connectors.whatsapp import WhatsAppConnector


def _collect(root: Path):
    recs, media = [], []
    for batch in WhatsAppConnector().parse_export(root):
        recs.extend(batch.records)
        media.extend(batch.media)
    return recs, media


def test_ios_24h_locale_dates(tmp_path):
    """iOS export: [M/D/YY, HH:MM:SS], BOM + LRM prefixes, multi-line, system msg."""
    root = tmp_path / "Alice"
    root.mkdir()
    (root / "_chat.txt").write_text(
        "\ufeff\u200e[1/29/18, 12:24:03] Messages and calls are end-to-end encrypted. "
        "No one outside of this chat, not even WhatsApp, can read or listen to them.\r\n"
        "[1/29/18, 12:24:10] Alice: hello\r\n"
        "[1/29/18, 12:25:44] Bob: first line\r\n"
        "second line\r\n",
        encoding="utf-8",
    )
    recs, media = _collect(root)
    assert len(recs) == 3
    system, alice, bob = recs
    # system message: matches the timestamp but has no "Name: " part
    assert system.author is None
    assert system.created_at == "2018-01-29T12:24:03"
    # US M/D/YY parses (the old ISO-only parser returned created_at=None here)
    assert alice.author == "Alice" and alice.text == "hello"
    assert alice.created_at == "2018-01-29T12:24:10"
    # non-header lines join the previous message (no stray \r from CRLF)
    assert bob.text == "first line\nsecond line"
    assert media == []


def test_ios_ampm_attached_and_omitted_media(tmp_path):
    """iOS with U+202F before AM/PM, LRM before <attached: X>, and 'image omitted'."""
    root = tmp_path / "Trip"
    root.mkdir()
    img = "00000035-PHOTO-2022-03-27-21-41-55.jpg"
    (root / img).write_bytes(b"\xff\xd8\xff\xe0JPEG")
    (root / "_chat.txt").write_text(
        f"[3/27/22, 9:41:55\u202fPM] Alice: \u200e<attached: {img}>\n"
        "\u200e[3/27/22, 9:42:10\u202fPM] Alice: \u200eimage omitted\n"
        "[3/28/22, 12:05:10\u202fAM] Bob: nice pic\n",
        encoding="utf-8",
    )
    recs, media = _collect(root)
    assert len(recs) == 3
    # 12h time with narrow no-break space: 9:41 PM -> 21:41
    assert recs[0].created_at == "2022-03-27T21:41:55"
    assert recs[0].author == "Alice"
    # 12:05 AM -> 00:05
    assert recs[2].created_at == "2022-03-28T00:05:10"
    # included media resolves to the real file next to _chat.txt
    assert len(media) == 1
    ref = media[0]
    assert ref.filename == img
    assert ref.owner_uid == recs[0].uid
    assert ref.kind == "image"
    assert Path(ref.source_path).read_bytes().startswith(b"\xff\xd8")
    # omitted media keeps the text and produces no MediaRef
    assert recs[1].text == "image omitted"


def test_android_dash_format_media(tmp_path):
    """Android: 'D/M/YY, HH:MM - Name: text', <Media omitted>, and (file attached)."""
    root = tmp_path / "wa"
    root.mkdir()
    (root / "IMG-20210428-WA0001.jpg").write_bytes(b"jpegdata")
    (root / "WhatsApp Chat with Alice.txt").write_text(
        "1/29/18, 12:24 - Messages to this chat and calls are now secured "
        "with end-to-end encryption.\n"
        "1/29/18, 12:24 - Alice: hello\n"
        "1/29/18, 1:07 PM - Bob: <Media omitted>\n"
        "4/28/21, 9:15 AM - Alice: IMG-20210428-WA0001.jpg (file attached)\n",
        encoding="utf-8",
    )
    recs, media = _collect(root)
    assert len(recs) == 4
    assert recs[0].author is None  # system line
    assert recs[1].created_at == "2018-01-29T12:24:00"
    assert recs[1].thread == "Alice"
    # excluded media: text kept, no MediaRef
    assert recs[2].text == "<Media omitted>"
    assert recs[2].created_at == "2018-01-29T13:07:00"
    # included media: MediaRef next to the .txt
    assert recs[3].created_at == "2021-04-28T09:15:00"
    assert len(media) == 1
    assert media[0].filename == "IMG-20210428-WA0001.jpg"
    assert media[0].owner_uid == recs[3].uid
    assert Path(media[0].source_path).read_bytes() == b"jpegdata"


def test_eu_day_first_dates(tmp_path):
    """German dotted D.M.YY and EU D/M/YYYY dates, incl. day>12 disambiguation."""
    de = tmp_path / "de"
    de.mkdir()
    (de / "WhatsApp Chat mit Karl.txt").write_text(
        "15.03.21, 14:07 - Karl: Hallo\n"  # day > 12 -> unambiguously day-first
        "03.04.21, 09:00 - Karl: Zweite Nachricht\n",  # ambiguous dotted -> day-first
        encoding="utf-8",
    )
    fr = tmp_path / "fr"
    fr.mkdir()
    (fr / "_chat.txt").write_text(
        "[25/12/2020, 20:15:00] Marie: joyeux noel\n"  # D/M/YYYY, day > 12
        "[13/1/21, 6:24:03 p.m.] Marie: bonsoir\n",  # p.m. with periods
        encoding="utf-8",
    )
    recs, _ = _collect(tmp_path)
    by_text = {r.text: r for r in recs}
    assert by_text["Hallo"].created_at == "2021-03-15T14:07:00"
    assert by_text["Zweite Nachricht"].created_at == "2021-04-03T09:00:00"
    assert by_text["joyeux noel"].created_at == "2020-12-25T20:15:00"
    assert by_text["bonsoir"].created_at == "2021-01-13T18:24:03"


def test_unparseable_date_keeps_message(tmp_path):
    """A timestamp the parser can't decode must not drop the message."""
    root = tmp_path / "odd"
    root.mkdir()
    (root / "WhatsApp Chat with X.txt").write_text(
        "13/13/9999, 99:99 - Alice: still here\n",
        encoding="utf-8",
    )
    recs, _ = _collect(root)
    assert len(recs) == 1
    assert recs[0].text == "still here"
    assert recs[0].created_at  # raw string kept, not None
