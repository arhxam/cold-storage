"""End-to-end CLI tests via Typer's runner + a real temp home."""

from __future__ import annotations

from typer.testing import CliRunner

from coldstorage.cli import app

runner = CliRunner()


def test_init_then_ingest_status_search(home, instagram_export, monkeypatch):
    # init (encrypted, passphrase provided so no prompt; keychain disabled in tests
    # so we must pass the passphrase to later commands via env)
    r = runner.invoke(app, ["init", "--passphrase", "swordfish"])
    assert r.exit_code == 0, r.output
    assert "Recovery Kit" in r.output or "RECOVERY KIT" in r.output
    assert str(home) in r.output

    monkeypatch.setenv("COLD_PASSPHRASE", "swordfish")

    r = runner.invoke(app, ["ingest", str(instagram_export)])
    assert r.exit_code == 0, r.output
    assert "Backed up" in r.output
    assert "instagram" in r.output

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "instagram" in r.output
    assert "encrypted" in r.output

    r = runner.invoke(app, ["search", "café"])
    assert r.exit_code == 0, r.output
    assert "instagram" in r.output


def test_init_refuses_second_time(home):
    assert runner.invoke(app, ["init", "--passphrase", "a"]).exit_code == 0
    r = runner.invoke(app, ["init", "--passphrase", "b"])
    assert r.exit_code != 0
    assert "already initialized" in r.output


def test_ingest_before_init_fails(home, instagram_export):
    r = runner.invoke(app, ["ingest", str(instagram_export)])
    assert r.exit_code != 0
    assert "init" in r.output.lower()


def test_no_encrypt_mode(home, instagram_export):
    r = runner.invoke(app, ["init", "--no-encrypt"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["ingest", str(instagram_export)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["status"])
    assert "not encrypted" in r.output


def test_where_and_connectors(home):
    runner.invoke(app, ["init", "--no-encrypt"])
    r = runner.invoke(app, ["where"])
    assert r.exit_code == 0
    assert str(home) in r.output

    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0
    for platform in ("instagram", "facebook", "discord", "twitter"):
        assert platform in r.output


def test_recover_resets_passphrase(home, instagram_export, monkeypatch):
    r = runner.invoke(app, ["init", "--passphrase", "original"])
    assert r.exit_code == 0
    # pull the recovery code out of the output
    import re

    codes = re.findall(r"[A-Z2-7]{4}(?:-[A-Z2-7]{4}){5,}", r.output)
    assert codes, r.output
    code = codes[0]

    r = runner.invoke(app, ["recover", code, "--new-passphrase", "fresh"])
    assert r.exit_code == 0, r.output

    monkeypatch.setenv("COLD_PASSPHRASE", "fresh")
    r = runner.invoke(app, ["ingest", str(instagram_export)])
    assert r.exit_code == 0, r.output


def test_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert "Cold Storage" in r.output


def test_status_json(home, instagram_export, monkeypatch):
    runner.invoke(app, ["init", "--no-encrypt"])
    runner.invoke(app, ["ingest", str(instagram_export)])
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    import json

    data = json.loads(r.output)
    assert data["total_records"] > 0
    assert any(c["connector"] == "instagram" for c in data["connectors"])


def test_doctor(home):
    runner.invoke(app, ["init", "--no-encrypt"])
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0
    assert "initialized" in r.output
    assert "restic" in r.output


def test_sync_without_restic_is_graceful(home, monkeypatch):
    # restic isn't installed in the test/CI environment → friendly failure, not a crash
    monkeypatch.setattr("shutil.which", lambda name: None)
    runner.invoke(app, ["init", "--no-encrypt"])
    r = runner.invoke(app, ["sync"])
    assert r.exit_code != 0
    assert "restic" in r.output.lower()


def test_schedule_prints_or_installs(home, tmp_path, monkeypatch):
    # never touch the real ~/Library/LaunchAgents
    monkeypatch.setenv("COLD_LAUNCHAGENTS_DIR", str(tmp_path / "LaunchAgents"))
    runner.invoke(app, ["init", "--no-encrypt"])
    r = runner.invoke(app, ["schedule", "--every", "daily"])
    assert r.exit_code == 0
    # some actionable output regardless of platform
    assert any(w in r.output.lower() for w in ("launchd", "cron", "schtasks", "reminder"))
