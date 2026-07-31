"""Optional versioned backup (restic) and cloud sync (rclone) for the archive.

Both tools are optional external binaries. Wrappers detect their absence via
:func:`shutil.which` and raise :class:`SyncToolMissing` instead of failing with
``FileNotFoundError``. All commands run through an injectable runner so callers
and tests can intercept the argv without spawning a process.
"""

from __future__ import annotations

from saveyourshit.sync.backup import RESTIC_BIN, ResticRepo, Runner, RunResult, SyncToolMissing
from saveyourshit.sync.cloud import RCLONE_BIN, RcloneRemote

__all__ = [
    "RCLONE_BIN",
    "RESTIC_BIN",
    "RcloneRemote",
    "ResticRepo",
    "RunResult",
    "Runner",
    "SyncToolMissing",
]
