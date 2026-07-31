"""An export we can't read is a normal thing for a user to hit.

It must explain itself and exit cleanly — never print a Python traceback — and
it must be identifiable as *permanent* so the desktop app stops retrying it.
"""

import zipfile

from typer.testing import CliRunner

from saveyourshit.cli import UNRECOGNIZED_MARKER, app
from saveyourshit.preflight import unrecognized_reasons

runner = CliRunner()


def _html_export(tmp_path):
    z = tmp_path / "instagram-html-export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("messages/inbox/friend/message_1.html", "<html>hi</html>")
    return z


def test_unrecognized_reasons_names_the_html_mistake(tmp_path):
    reasons = unrecognized_reasons(_html_export(tmp_path))
    assert any("HTML export" in r for r in reasons)
    assert any("JSON" in r for r in reasons)


def test_unrecognized_reasons_handles_a_corrupt_zip(tmp_path):
    bad = tmp_path / "half-downloaded.zip"
    bad.write_bytes(b"not really a zip")
    assert any("corrupt" in r or "downloading" in r for r in unrecognized_reasons(bad))


def test_unrecognized_reasons_suggests_connector_when_not_html(tmp_path):
    z = tmp_path / "mystery.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("data/whatever.json", "{}")
    assert any("--connector" in r for r in unrecognized_reasons(z))


def test_ingest_explains_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("SYT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SYT_NO_KEYRING", "1")
    assert runner.invoke(app, ["init", "--no-encrypt"]).exit_code == 0

    result = runner.invoke(app, ["ingest", str(_html_export(tmp_path)), "--no-snapshot"])

    assert result.exit_code == 2, "a clean exit code, not a crash"
    assert "Traceback" not in result.output
    assert "HTML export" in result.output
    # The app keys off this marker to stop retrying a file that can never work.
    assert UNRECOGNIZED_MARKER in result.output
