"""Cloud sync of the archive tree via `rclone <https://rclone.org>`_.

rclone is an *optional* external binary (like restic in
:mod:`saveyourshit.sync.backup`). It moves an already-encrypted local tree to
the user's *own* cloud storage (B2, Drive, Dropbox, S3, ...) — consistent with
the local-first promise: the only network traffic is to storage the user
controls.

The same injectable-runner pattern is used so tests never spawn a process.
Remote configuration (credentials, endpoints) stays in the user's own rclone
config; this wrapper only builds argv lists like ``rclone sync <src> <remote>``.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from saveyourshit.sync.backup import Runner, RunResult, SyncToolMissing, _default_runner

RCLONE_BIN = "rclone"


class RcloneRemote:
    """Thin wrapper around the rclone CLI.

    Parameters
    ----------
    binary:
        Name or path of the rclone executable (defaults to ``"rclone"``).
    runner:
        Callable that actually executes the argv; defaults to a subprocess
        runner. Tests inject a fake here so no process is ever spawned.
    """

    def __init__(self, binary: str = RCLONE_BIN, runner: Runner | None = None) -> None:
        self.binary = binary
        self._run: Runner = runner or _default_runner

    # -- availability --------------------------------------------------------

    def is_available(self) -> bool:
        """True if the rclone binary can be found on PATH."""
        return shutil.which(self.binary) is not None

    def _exec(self, args: Sequence[str]) -> RunResult:
        if not self.is_available():
            raise SyncToolMissing(self.binary)
        return self._run([self.binary, *args], None)

    # -- commands ------------------------------------------------------------

    def sync(self, local_path: str | Path, remote: str) -> RunResult:
        """Make ``remote`` identical to ``local_path`` (``rclone sync``; deletes extras)."""
        return self._exec(["sync", str(local_path), remote])

    def copy(self, local_path: str | Path, remote: str) -> RunResult:
        """Copy ``local_path`` to ``remote`` (``rclone copy``; never deletes on the remote)."""
        return self._exec(["copy", str(local_path), remote])

    def list_remotes(self) -> RunResult:
        """List configured remotes (``rclone listremotes``); parse ``result.stdout``."""
        return self._exec(["listremotes"])
