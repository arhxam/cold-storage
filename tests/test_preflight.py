from saveyourshit.preflight import check_export


def test_check_recognizes_instagram(instagram_export):
    r = check_export(instagram_export)
    assert r.connector == "instagram"
    assert r.counts.get("message", 0) > 0
    assert r.counts.get("follower", 0) > 0
    assert r.threads >= 1
    assert not r.warnings  # a healthy export → no warnings


def test_check_unrecognized_reports_no_connector(tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "random.txt").write_text("nothing")
    r = check_export(junk)
    assert r.connector is None
    assert any("not recognized" in w.lower() for w in r.warnings)


def test_check_warns_on_html_export(tmp_path):
    html = tmp_path / "ig-html"
    (html / "your_instagram_activity/messages/inbox/x").mkdir(parents=True)
    (html / "your_instagram_activity/messages/inbox/x/message_1.html").write_text("<html></html>")
    r = check_export(html)
    # detects the platform but finds no JSON data → loud HTML hint (0 records)
    assert r.total == 0
    assert any("html" in w.lower() for w in r.warnings)


def test_check_from_zip(instagram_export, tmp_path):
    import zipfile

    z = tmp_path / "ig.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in instagram_export.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(instagram_export))
    r = check_export(z)
    assert r.connector == "instagram"
    assert r.total > 0
