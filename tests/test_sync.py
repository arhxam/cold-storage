"""Tests for the restic/rclone wrappers.

The real binaries are NOT installed (and must not be needed): every test either
injects a fake runner that records the argv it was handed, or monkeypatches
``shutil.which`` to simulate presence/absence of the tool.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from saveyourshit.sync import RcloneRemote, ResticRepo, RunResult, SyncToolMissing


class FakeRunner:
    """Records every (argv, extra_env) call and returns a canned result."""

    def __init__(self, result: RunResult | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.result = result or RunResult(returncode=0, stdout="ok")

    def __call__(self, argv, extra_env=None) -> RunResult:
        self.calls.append((list(argv), dict(extra_env) if extra_env is not None else None))
        return self.result

    @property
    def last_argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def last_env(self) -> dict[str, str] | None:
        return self.calls[-1][1]


@pytest.fixture
def tool_installed(monkeypatch):
    """Pretend restic/rclone are on PATH (they are not on this machine)."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")


@pytest.fixture
def tool_missing(monkeypatch):
    """Pretend no external tool is on PATH."""
    monkeypatch.setattr(shutil, "which", lambda name: None)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def repo(tool_installed, runner) -> ResticRepo:
    return ResticRepo("/backups/syt-repo", runner=runner)


@pytest.fixture
def remote(tool_installed, runner) -> RcloneRemote:
    return RcloneRemote(runner=runner)


# -- restic: argv construction ----------------------------------------------


class TestResticArgv:
    def test_init(self, repo, runner):
        result = repo.init("hunter2")
        assert runner.last_argv == ["restic", "--repo", "/backups/syt-repo", "init"]
        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.ok

    def test_password_goes_through_env_not_argv(self, repo, runner):
        repo.init("hunter2")
        assert runner.last_env == {"RESTIC_PASSWORD": "hunter2"}
        assert "hunter2" not in " ".join(runner.last_argv)

    def test_snapshot_single_path(self, repo, runner):
        repo.snapshot(["/home/me/SaveYourShit"], "pw")
        assert runner.last_argv == [
            "restic", "--repo", "/backups/syt-repo", "backup", "/home/me/SaveYourShit",
        ]
        assert runner.last_env == {"RESTIC_PASSWORD": "pw"}

    def test_snapshot_multiple_paths_accepts_pathlib(self, repo, runner):
        repo.snapshot([Path("/data/a"), "/data/b"], "pw")
        assert runner.last_argv == [
            "restic", "--repo", "/backups/syt-repo", "backup", "/data/a", "/data/b",
        ]

    def test_snapshot_with_tags(self, repo, runner):
        repo.snapshot(["/data"], "pw", tags=["syt", "nightly"])
        assert runner.last_argv == [
            "restic", "--repo", "/backups/syt-repo", "backup",
            "--tag", "syt", "--tag", "nightly", "/data",
        ]

    def test_snapshot_no_tags_adds_no_tag_flags(self, repo, runner):
        repo.snapshot(["/data"], "pw", tags=None)
        assert "--tag" not in runner.last_argv

    def test_check(self, repo, runner):
        repo.check("pw")
        assert runner.last_argv == ["restic", "--repo", "/backups/syt-repo", "check"]
        assert runner.last_env == {"RESTIC_PASSWORD": "pw"}

    def test_list_snapshots_requests_json(self, repo, runner):
        repo.list_snapshots("pw")
        assert runner.last_argv == [
            "restic", "--repo", "/backups/syt-repo", "snapshots", "--json",
        ]

    def test_restore(self, repo, runner):
        repo.restore("ab12cd34", Path("/tmp/restored"), "pw")
        assert runner.last_argv == [
            "restic", "--repo", "/backups/syt-repo",
            "restore", "ab12cd34", "--target", "/tmp/restored",
        ]
        assert runner.last_env == {"RESTIC_PASSWORD": "pw"}

    def test_restore_latest(self, repo, runner):
        repo.restore("latest", "/tmp/out", "pw")
        assert runner.last_argv[3:5] == ["restore", "latest"]

    def test_repo_accepts_pathlib(self, tool_installed, runner):
        repo = ResticRepo(Path("/somewhere/repo"), runner=runner)
        repo.init("pw")
        assert runner.last_argv == ["restic", "--repo", "/somewhere/repo", "init"]

    def test_custom_binary_used_in_argv(self, tool_installed, runner):
        repo = ResticRepo("/r", binary="/opt/syt/bin/restic", runner=runner)
        repo.check("pw")
        assert runner.last_argv[0] == "/opt/syt/bin/restic"

    def test_result_is_passed_through_from_runner(self, tool_installed):
        canned = RunResult(returncode=1, stdout="", stderr="repo locked")
        repo = ResticRepo("/r", runner=FakeRunner(canned))
        result = repo.check("pw")
        assert result is canned
        assert not result.ok


# -- rclone: argv construction ----------------------------------------------


class TestRcloneArgv:
    def test_sync(self, remote, runner):
        result = remote.sync("/home/me/SaveYourShit", "b2:my-bucket/syt")
        assert runner.last_argv == [
            "rclone", "sync", "/home/me/SaveYourShit", "b2:my-bucket/syt",
        ]
        assert runner.last_env is None
        assert result.ok

    def test_sync_accepts_pathlib(self, remote, runner):
        remote.sync(Path("/data/archive"), "drive:backups")
        assert runner.last_argv == ["rclone", "sync", "/data/archive", "drive:backups"]

    def test_copy(self, remote, runner):
        remote.copy("/data/archive", "dropbox:syt")
        assert runner.last_argv == ["rclone", "copy", "/data/archive", "dropbox:syt"]

    def test_list_remotes(self, remote, runner):
        remote.list_remotes()
        assert runner.last_argv == ["rclone", "listremotes"]

    def test_custom_binary_used_in_argv(self, tool_installed, runner):
        remote = RcloneRemote(binary="/opt/syt/bin/rclone", runner=runner)
        remote.list_remotes()
        assert runner.last_argv[0] == "/opt/syt/bin/rclone"


# -- missing binaries --------------------------------------------------------


class TestToolMissing:
    def test_restic_is_available_false(self, tool_missing, runner):
        assert ResticRepo("/r", runner=runner).is_available() is False

    def test_rclone_is_available_false(self, tool_missing, runner):
        assert RcloneRemote(runner=runner).is_available() is False

    def test_is_available_true_when_on_path(self, tool_installed, runner):
        assert ResticRepo("/r", runner=runner).is_available() is True
        assert RcloneRemote(runner=runner).is_available() is True

    @pytest.mark.parametrize("op", ["init", "check", "list_snapshots"])
    def test_restic_simple_ops_raise(self, tool_missing, runner, op):
        repo = ResticRepo("/r", runner=runner)
        with pytest.raises(SyncToolMissing, match="restic"):
            getattr(repo, op)("pw")

    def test_restic_snapshot_raises(self, tool_missing, runner):
        with pytest.raises(SyncToolMissing, match="restic"):
            ResticRepo("/r", runner=runner).snapshot(["/data"], "pw")

    def test_restic_restore_raises(self, tool_missing, runner):
        with pytest.raises(SyncToolMissing, match="restic"):
            ResticRepo("/r", runner=runner).restore("latest", "/tmp/out", "pw")

    def test_rclone_ops_raise(self, tool_missing, runner):
        remote = RcloneRemote(runner=runner)
        with pytest.raises(SyncToolMissing, match="rclone"):
            remote.sync("/data", "b2:bucket")
        with pytest.raises(SyncToolMissing, match="rclone"):
            remote.copy("/data", "b2:bucket")
        with pytest.raises(SyncToolMissing, match="rclone"):
            remote.list_remotes()

    def test_missing_tool_never_invokes_runner(self, tool_missing, runner):
        with pytest.raises(SyncToolMissing):
            ResticRepo("/r", runner=runner).init("pw")
        with pytest.raises(SyncToolMissing):
            RcloneRemote(runner=runner).sync("/a", "b2:b")
        assert runner.calls == []

    def test_exception_names_the_tool_and_is_actionable(self, tool_missing, runner):
        with pytest.raises(SyncToolMissing) as exc_info:
            ResticRepo("/r", runner=runner).init("pw")
        assert exc_info.value.tool == "restic"
        assert "optional" in str(exc_info.value)
