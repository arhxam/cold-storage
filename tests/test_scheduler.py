"""Tests for coldstorage.scheduler — pure scheduling logic and OS artifact generators.

No test touches the real LaunchAgents directory, crontab, or Task Scheduler.
"""

from __future__ import annotations

import plistlib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest

from coldstorage.scheduler import (
    cron_line,
    install_macos,
    is_due,
    launchd_plist,
    next_run,
    schtasks_command,
    uninstall_macos,
)

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- next_run


class TestNextRun:
    def test_daily_adds_one_day(self):
        last = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
        assert next_run("daily", last, NOW) == datetime(2026, 7, 31, 3, 0, tzinfo=UTC)

    def test_weekly_adds_seven_days(self):
        last = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)
        assert next_run("weekly", last, NOW) == datetime(2026, 7, 31, 3, 0, tzinfo=UTC)

    def test_last_none_is_due_now(self):
        assert next_run("daily", None, NOW) == NOW
        assert next_run("weekly", None, NOW) == NOW

    def test_manual_raises(self):
        with pytest.raises(ValueError, match="manual"):
            next_run("manual", None, NOW)
        with pytest.raises(ValueError, match="manual"):
            next_run("manual", NOW - timedelta(days=1), NOW)

    def test_unknown_schedule_raises(self):
        with pytest.raises(ValueError, match="hourly"):
            next_run("hourly", None, NOW)

    def test_pure_does_not_depend_on_wall_clock(self):
        last = datetime(2000, 1, 1, tzinfo=UTC)
        assert next_run("daily", last, NOW) == datetime(2000, 1, 2, tzinfo=UTC)

    def test_preserves_timezone_awareness(self):
        last = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
        assert next_run("weekly", last, NOW).tzinfo is UTC


# ---------------------------------------------------------------- is_due


class TestIsDue:
    def test_last_none_always_due(self):
        assert is_due("daily", None, NOW) is True
        assert is_due("weekly", None, NOW) is True

    def test_daily_not_due_before_interval(self):
        last = NOW - timedelta(hours=23, minutes=59)
        assert is_due("daily", last, NOW) is False

    def test_daily_due_exactly_at_boundary(self):
        last = NOW - timedelta(days=1)
        assert is_due("daily", last, NOW) is True

    def test_daily_due_after_interval(self):
        last = NOW - timedelta(days=1, seconds=1)
        assert is_due("daily", last, NOW) is True

    def test_weekly_not_due_before_interval(self):
        last = NOW - timedelta(days=6, hours=23)
        assert is_due("weekly", last, NOW) is False

    def test_weekly_due_exactly_at_boundary(self):
        last = NOW - timedelta(weeks=1)
        assert is_due("weekly", last, NOW) is True

    def test_weekly_due_after_interval(self):
        last = NOW - timedelta(days=10)
        assert is_due("weekly", last, NOW) is True

    def test_manual_never_due(self):
        assert is_due("manual", None, NOW) is False
        assert is_due("manual", NOW - timedelta(days=365), NOW) is False

    def test_unknown_schedule_raises(self):
        with pytest.raises(ValueError, match="fortnightly"):
            is_due("fortnightly", None, NOW)

    def test_future_last_not_due(self):
        # Clock skew / restored state: last in the future must not be due.
        assert is_due("daily", NOW + timedelta(hours=1), NOW) is False


# ---------------------------------------------------------------- launchd_plist


class TestLaunchdPlist:
    ARGS = ["/usr/local/bin/cold", "run", "--all"]

    def test_is_well_formed_xml(self):
        xml = launchd_plist("com.coldstorage.backup", self.ARGS, 86400)
        root = ET.fromstring(xml)
        assert root.tag == "plist"

    def test_contains_program_args_and_interval(self):
        xml = launchd_plist("com.coldstorage.backup", self.ARGS, 86400)
        data = plistlib.loads(xml.encode("utf-8"))
        assert data["Label"] == "com.coldstorage.backup"
        assert data["ProgramArguments"] == self.ARGS
        assert data["StartInterval"] == 86400
        assert data["RunAtLoad"] is False

    def test_start_interval_key_present_in_text(self):
        xml = launchd_plist("com.coldstorage.backup", self.ARGS, 3600)
        assert "StartInterval" in xml
        for arg in self.ARGS:
            assert arg in xml

    def test_empty_program_args_raises(self):
        with pytest.raises(ValueError, match="program_args"):
            launchd_plist("com.coldstorage.backup", [], 3600)

    def test_nonpositive_interval_raises(self):
        with pytest.raises(ValueError, match="interval_seconds"):
            launchd_plist("com.coldstorage.backup", self.ARGS, 0)
        with pytest.raises(ValueError, match="interval_seconds"):
            launchd_plist("com.coldstorage.backup", self.ARGS, -5)

    def test_args_with_xml_special_chars_survive_roundtrip(self):
        args = ["/bin/sh", "-c", "cold run && echo '<done>'"]
        xml = launchd_plist("com.coldstorage.backup", args, 60)
        data = plistlib.loads(xml.encode("utf-8"))
        assert data["ProgramArguments"] == args


# ---------------------------------------------------------------- cron_line


class TestCronLine:
    def test_daily(self):
        assert cron_line("daily", "cold run") == "0 3 * * * cold run"

    def test_weekly(self):
        assert cron_line("weekly", "cold run") == "0 3 * * 0 cold run"

    def test_has_five_time_fields(self):
        fields = cron_line("daily", "cold").split()
        assert fields[:5] == ["0", "3", "*", "*", "*"]

    def test_manual_raises(self):
        with pytest.raises(ValueError, match="manual"):
            cron_line("manual", "cold run")

    def test_unknown_schedule_raises(self):
        with pytest.raises(ValueError):
            cron_line("monthly", "cold run")


# ---------------------------------------------------------------- schtasks_command


class TestSchtasksCommand:
    def test_daily_shape(self):
        argv = schtasks_command("ColdStorage", "cold run", "daily")
        assert argv[0] == "schtasks"
        assert argv[1] == "/Create"
        assert argv[argv.index("/TN") + 1] == "ColdStorage"
        assert argv[argv.index("/TR") + 1] == "cold run"
        assert argv[argv.index("/SC") + 1] == "DAILY"
        assert "/F" in argv

    def test_weekly_schedule_flag(self):
        argv = schtasks_command("ColdStorage", "cold run", "weekly")
        assert argv[argv.index("/SC") + 1] == "WEEKLY"

    def test_all_elements_are_strings(self):
        argv = schtasks_command("ColdStorage", "cold run", "daily")
        assert all(isinstance(part, str) for part in argv)

    def test_manual_raises(self):
        with pytest.raises(ValueError, match="manual"):
            schtasks_command("ColdStorage", "cold run", "manual")


# ---------------------------------------------------------------- install/uninstall


class TestInstallMacos:
    def test_writes_plist_into_injected_dir(self, tmp_path):
        path = install_macos(
            "com.coldstorage.backup", ["/usr/local/bin/cold", "run"], 86400, plist_dir=tmp_path
        )
        assert path == tmp_path / "com.coldstorage.backup.plist"
        assert path.is_file()
        data = plistlib.loads(path.read_bytes())
        assert data["ProgramArguments"] == ["/usr/local/bin/cold", "run"]
        assert data["StartInterval"] == 86400

    def test_creates_missing_directory(self, tmp_path):
        target = tmp_path / "nested" / "LaunchAgents"
        path = install_macos("com.coldstorage.backup", ["cold"], 60, plist_dir=target)
        assert path.is_file()
        assert path.parent == target

    def test_overwrites_existing_plist(self, tmp_path):
        install_macos("com.coldstorage.backup", ["cold", "old"], 60, plist_dir=tmp_path)
        path = install_macos("com.coldstorage.backup", ["cold", "new"], 120, plist_dir=tmp_path)
        data = plistlib.loads(path.read_bytes())
        assert data["ProgramArguments"] == ["cold", "new"]
        assert data["StartInterval"] == 120

    def test_uninstall_removes_file(self, tmp_path):
        path = install_macos("com.coldstorage.backup", ["cold"], 60, plist_dir=tmp_path)
        assert uninstall_macos("com.coldstorage.backup", plist_dir=tmp_path) is True
        assert not path.exists()

    def test_uninstall_missing_returns_false(self, tmp_path):
        assert uninstall_macos("com.coldstorage.nothere", plist_dir=tmp_path) is False
