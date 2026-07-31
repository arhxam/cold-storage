"""Versioned, encrypted backups of the archive via `restic <https://restic.net>`_.

restic is an *optional* external binary: nothing in the core requires it, and this
module must degrade gracefully when it is absent. Every command therefore checks
:meth:`ResticRepo.is_available` first and raises :class:`SyncToolMissing` with an
actionable message instead of a cryptic ``FileNotFoundError``.

All commands are built as plain argv lists and executed through an injectable
``runner`` callable so tests (and callers who want dry runs) never have to spawn
a real process. The repository password is handed to restic via the
``RESTIC_PASSWORD`` environment variable — it never appears in the argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

RESTIC_BIN = "restic"


class SyncToolMissing(RuntimeError):
    """An optional external sync tool (restic / rclone) is not installed."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(
            f"'{tool}' was not found on PATH. It is an optional tool — install it "
            f"(or bundle it next to syt) to enable this feature."
        )


@dataclass(frozen=True)
class RunResult:
    """Outcome of one external command."""

    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# A runner executes an argv with (optionally) extra environment variables layered
# on top of the current environment, and reports the outcome. Injecting one lets
# tests capture the argv instead of spawning a process.
Runner = Callable[[Sequence[str], Mapping[str, str] | None], RunResult]


def _default_runner(argv: Sequence[str], extra_env: Mapping[str, str] | None = None) -> RunResult:
    """Run ``argv`` via :func:`subprocess.run`, capturing output as text."""
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.run(list(argv), env=env, capture_output=True, text=True, check=False)
    return RunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class ResticRepo:
    """A restic repository (local path or restic-supported URL, e.g. ``rclone:remote:bucket``).

    Parameters
    ----------
    repo:
        Repository location, passed to every command as ``--repo``.
    binary:
        Name or path of the restic executable (defaults to ``"restic"``).
    runner:
        Callable that actually executes the argv; defaults to a subprocess
        runner. Tests inject a fake here so no process is ever spawned.
    """

    def __init__(
        self,
        repo: str | Path,
        binary: str = RESTIC_BIN,
        runner: Runner | None = None,
    ) -> None:
        self.repo = str(repo)
        self.binary = binary
        self._run: Runner = runner or _default_runner

    # -- availability --------------------------------------------------------

    def is_available(self) -> bool:
        """True if the restic binary can be found on PATH."""
        return shutil.which(self.binary) is not None

    def _exec(self, args: Sequence[str], password: str) -> RunResult:
        if not self.is_available():
            raise SyncToolMissing(self.binary)
        argv = [self.binary, "--repo", self.repo, *args]
        return self._run(argv, {"RESTIC_PASSWORD": password})

    # -- commands ------------------------------------------------------------

    def init(self, password: str) -> RunResult:
        """Create (initialize) the repository."""
        return self._exec(["init"], password)

    def snapshot(
        self,
        paths: Sequence[str | Path],
        password: str,
        tags: Sequence[str] | None = None,
    ) -> RunResult:
        """Back up ``paths`` as a new snapshot (``restic backup``), optionally tagged."""
        args: list[str] = ["backup"]
        for tag in tags or ():
            args += ["--tag", tag]
        args += [str(p) for p in paths]
        return self._exec(args, password)

    def check(self, password: str) -> RunResult:
        """Verify repository integrity (``restic check``)."""
        return self._exec(["check"], password)

    def list_snapshots(self, password: str) -> RunResult:
        """List snapshots as JSON (``restic snapshots --json``); parse ``result.stdout``."""
        return self._exec(["snapshots", "--json"], password)

    def restore(self, snapshot_id: str, target: str | Path, password: str) -> RunResult:
        """Restore ``snapshot_id`` (or ``"latest"``) into the ``target`` directory."""
        return self._exec(["restore", snapshot_id, "--target", str(target)], password)
