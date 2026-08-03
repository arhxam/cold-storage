"""Telegram Desktop's default export is HTML — it must parse, not read as empty."""

from coldstorage.connectors.telegram import TelegramConnector

MSGS = """<html><body>
<div class="page_header"><div class="content"><div class="text bold">Alice</div></div></div>
<div class="history">
<div class="message default clearfix" id="m1"><div class="body">
  <div class="pull_right date details" title="01.01.2024 12:34:56 UTC+00:00">12:34</div>
  <div class="from_name">Alice</div><div class="text">Hey there!</div></div></div>
<div class="message default clearfix joined" id="m2"><div class="body">
  <div class="pull_right date details" title="01.01.2024 12:35:10 UTC+00:00">12:35</div>
  <div class="text">how are you</div></div></div>
<div class="message service" id="s3"><div class="body">Alice joined Telegram</div></div>
</div></body></html>"""


def _export(tmp_path):
    chat = tmp_path / "chats" / "chat_1"
    chat.mkdir(parents=True)
    (tmp_path / "export_results.html").write_text("<html>Telegram</html>")
    (chat / "messages.html").write_text(MSGS)
    return tmp_path


def test_detects_html_export(tmp_path):
    assert TelegramConnector().detect(_export(tmp_path))


def test_parses_messages_joined_and_timestamp(tmp_path):
    records = [r for b in TelegramConnector().parse_export(_export(tmp_path)) for r in b.records]
    assert len(records) == 2  # the service (join) row is skipped
    # The "joined" second message omits from_name and inherits the sender.
    assert [r.author for r in records] == ["Alice", "Alice"]
    assert records[0].created_at == "2024-01-01T12:34:56+00:00"
    assert records[0].thread == "Alice"  # chat name from the page header
