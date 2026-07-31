from saveyourshit import connectors
from saveyourshit.connectors.base import detect_connector
from saveyourshit.models import RecordType
from saveyourshit.textutil import fix_meta_mojibake


def _collect(connector, root):
    recs, media = [], []
    for batch in connector.parse_export(root):
        recs += batch.records
        media += batch.media
    return recs, media


def test_mojibake_repair():
    assert fix_meta_mojibake("BeyoncÃ©") == "Beyoncé"
    assert fix_meta_mojibake("cafÃ©") == "café"
    assert fix_meta_mojibake("plain ascii") == "plain ascii"


def test_instagram_detect_and_parse(instagram_export):
    c = detect_connector(instagram_export)
    assert c is not None and c.id == "instagram"
    recs, media = _collect(c, instagram_export)
    kinds = {r.type for r in recs}
    assert RecordType.MESSAGE in kinds
    assert RecordType.FOLLOWER in kinds
    assert RecordType.FOLLOWING in kinds
    assert RecordType.POST in kinds
    # mojibake fixed in message text and thread title
    msgs = [r for r in recs if r.type == RecordType.MESSAGE]
    assert any(r.text == "café tomorrow?" for r in msgs)
    assert any(r.thread == "Beyoncé" for r in msgs)
    # media references resolved to real files
    assert any(m.source_path.endswith("photo1.jpg") for m in media)


def test_instagram_followers_values(instagram_export):
    c = connectors.get("instagram")
    recs, _ = _collect(c, instagram_export)
    followers = [r for r in recs if r.type == RecordType.FOLLOWER]
    following = [r for r in recs if r.type == RecordType.FOLLOWING]
    assert [r.text for r in followers] == ["alice"]
    assert [r.text for r in following] == ["bob"]


def test_facebook_detect_and_parse(facebook_export):
    c = detect_connector(facebook_export)
    assert c is not None and c.id == "facebook"
    recs, _ = _collect(c, facebook_export)
    assert any(r.type == RecordType.MESSAGE and r.text == "hey" for r in recs)
    assert any(r.type == RecordType.FOLLOWER and r.text == "Charlie" for r in recs)
    assert any(r.type == RecordType.POST and r.text == "hello world" for r in recs)


def test_discord_detect_and_parse(discord_export):
    c = detect_connector(discord_export)
    assert c is not None and c.id == "discord"
    recs, _ = _collect(c, discord_export)
    texts = {r.text for r in recs}
    assert texts == {"yo", "sup"}
    assert all(r.thread == "Direct Message with pal" for r in recs)


def test_twitter_detect_and_parse(twitter_export):
    c = detect_connector(twitter_export)
    assert c is not None and c.id == "twitter"
    recs, _ = _collect(c, twitter_export)
    assert any(r.type == RecordType.POST and r.text == "gm" for r in recs)
    assert any(r.type == RecordType.MESSAGE and r.text == "hi there" for r in recs)
    assert any(r.type == RecordType.FOLLOWER and r.text == "555" for r in recs)


def test_telegram_detect_and_parse(telegram_export):
    c = detect_connector(telegram_export)
    assert c is not None and c.id == "telegram"
    recs, _ = _collect(c, telegram_export)
    texts = {r.text for r in recs}
    assert "hi there" in texts
    # list-of-runs text is flattened, service messages skipped
    assert "check this out" in texts
    assert all(r.thread == "Pal" for r in recs)
    assert len(recs) == 2


def test_reddit_detect_and_parse(reddit_export):
    c = detect_connector(reddit_export)
    assert c is not None and c.id == "reddit"
    recs, _ = _collect(c, reddit_export)
    assert any(r.type == RecordType.COMMENT and r.text == "great post" for r in recs)
    assert any(r.type == RecordType.POST and r.text == "My title" for r in recs)
    assert any(r.thread == "python" for r in recs)


def test_whatsapp_detect_and_parse(whatsapp_export):
    c = detect_connector(whatsapp_export)
    assert c is not None and c.id == "whatsapp"
    recs, media = _collect(c, whatsapp_export)
    assert any(r.author == "Alice" and r.text == "Hello there" for r in recs)
    # multi-line continuation is joined onto the previous message
    assert any(r.author == "Me" and "second line" in (r.text or "") for r in recs)
    # attached media resolved to the file next to the txt
    assert any(m.source_path.endswith("IMG-0001.jpg") for m in media)


def test_all_connectors_registered():
    ids = {c.id for c in connectors.all_connectors()}
    expected = {"instagram", "facebook", "discord", "twitter", "telegram", "reddit", "whatsapp"}
    assert expected <= ids
