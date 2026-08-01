"""Scheduling logic for periodic ``cold`` runs — pure functions plus OS artifact generators.

This module never requires a running daemon. It computes when a backup is due
(:func:`next_run`, :func:`is_due`) and generates the OS-native artifacts that make the
platform's own scheduler invoke ``cold``:

- macOS: a launchd agent plist (:func:`launchd_plist`, :func:`install_macos`)
- Linux: a crontab line (:func:`cron_line`)
- Windows: a ``schtasks`` argv (:func:`schtasks_command`)

Everything except :func:`launchctl_load` / :func:`launchctl_unload` is side-effect free
or filesystem-injectable, so it is fully testable without touching the real system.
"""

from __future__ import annotations

import plistlib
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

__all__ = [
    "SCHEDULES",
    "cron_line",
    "install_macos",
    "is_due",
    "launchctl_load",
    "launchctl_unload",
    "launchd_plist",
    "next_run",
    "schtasks_command",
    "uninstall_macos",
]

#: Recognized schedule names. "manual" means the user runs ``cold`` themselves.
SCHEDULES: frozenset[str] = frozenset({"daily", "weekly", "manual"})

_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}

#: Hour of day (local time) used for cron / schtasks entries.
_RUN_HOUR = 3


def _validate_schedule(schedule: str) -> None:
    if schedule not in SCHEDULES:
        raise ValueError(f"unknown schedule {schedule!r}; expected one of {sorted(SCHEDULES)}")


def next_run(schedule: str, last: datetime | None, now: datetime) -> datetime:
    """Return the next moment a run is due.

    Pure function: depends only on its arguments.

    Args:
        schedule: "daily" or "weekly". "manual" raises ``ValueError`` because a
            manual schedule has no automatic next run.
        last: When the last run happened, or ``None`` if it never ran.
        now: The current time (pass an explicit tz-aware datetime).

    Returns:
        ``now`` when ``last`` is ``None`` (due immediately), otherwise
        ``last + interval`` for the given schedule.

    Raises:
        ValueError: If ``schedule`` is "manual" or unrecognized.
    """
    _validate_schedule(schedule)
    if schedule == "manual":
        raise ValueError("schedule 'manual' has no next run; runs are user-initiated")
    if last is None:
        return now
    return last + _INTERVALS[schedule]


def is_due(schedule: str, last: datetime | None, now: datetime) -> bool:
    """Return whether a run is due at ``now``.

    "manual" is never due automatically. ``last=None`` is always due. Otherwise a run
    is due once ``now`` reaches ``last + interval`` (boundary inclusive).

    Raises:
        ValueError: If ``schedule`` is unrecognized.
    """
    _validate_schedule(schedule)
    if schedule == "manual":
        return False
    return next_run(schedule, last, now) <= now


def launchd_plist(label: str, program_args: list[str], interval_seconds: int) -> str:
    """Return a launchd agent plist (XML string) that runs ``program_args`` periodically.

    Uses ``StartInterval`` so launchd wakes the job every ``interval_seconds`` without
    any daemon of our own. ``RunAtLoad`` is false: the first run happens one interval
    after load, not immediately.

    Args:
        label: Reverse-DNS launchd label, e.g. ``"com.coldstorage.backup"``.
        program_args: Full argv to execute, e.g. ``["/usr/local/bin/cold", "run"]``.
        interval_seconds: Seconds between runs; must be positive.

    Raises:
        ValueError: If ``program_args`` is empty or ``interval_seconds`` is not positive.
    """
    if not program_args:
        raise ValueError("program_args must not be empty")
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
    payload = {
        "Label": label,
        "ProgramArguments": list(program_args),
        "StartInterval": interval_seconds,
        "RunAtLoad": False,
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def cron_line(schedule: str, command: str) -> str:
    """Return a crontab line running ``command`` on the given schedule.

    daily → ``"0 3 * * * <command>"`` (03:00 every day);
    weekly → ``"0 3 * * 0 <command>"`` (03:00 every Sunday).

    Raises:
        ValueError: If ``schedule`` is "manual" or unrecognized.
    """
    _validate_schedule(schedule)
    if schedule == "manual":
        raise ValueError("schedule 'manual' has no cron entry; runs are user-initiated")
    dow = "*" if schedule == "daily" else "0"
    return f"0 {_RUN_HOUR} * * {dow} {command}"


def schtasks_command(name: str, command: str, schedule: str) -> list[str]:
    """Return the ``schtasks`` argv that registers a Windows scheduled task.

    daily/weekly map to ``/SC DAILY`` / ``/SC WEEKLY`` at 03:00; ``/F`` overwrites an
    existing task of the same name.

    Raises:
        ValueError: If ``schedule`` is "manual" or unrecognized.
    """
    _validate_schedule(schedule)
    if schedule == "manual":
        raise ValueError("schedule 'manual' has no scheduled task; runs are user-initiated")
    return [
        "schtasks",
        "/Create",
        "/TN",
        name,
        "/TR",
        command,
        "/SC",
        schedule.upper(),
        "/ST",
        f"{_RUN_HOUR:02d}:00",
        "/F",
    ]


def _default_plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str, plist_dir: Path | None) -> Path:
    directory = plist_dir if plist_dir is not None else _default_plist_dir()
    return directory / f"{label}.plist"


def install_macos(
    label: str,
    program_args: list[str],
    interval_seconds: int,
    *,
    plist_dir: Path | None = None,
) -> Path:
    """Write the launchd agent plist and return its path.

    Only writes the file — it does NOT load the agent; call :func:`launchctl_load`
    with the returned path for that.

    Args:
        plist_dir: Target directory; defaults to ``~/Library/LaunchAgents``. Inject a
            temp dir in tests.
    """
    path = _plist_path(label, plist_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launchd_plist(label, program_args, interval_seconds), encoding="utf-8")
    return path


def uninstall_macos(label: str, *, plist_dir: Path | None = None) -> bool:
    """Remove the agent plist for ``label`` if present; return True if a file was removed.

    Does NOT unload the agent; call :func:`launchctl_unload` first if it is loaded.
    """
    path = _plist_path(label, plist_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def launchctl_load(plist_path: Path) -> None:
    """Load the agent into launchd (side-effecting; never called from tests)."""
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)  # pragma: no cover


def launchctl_unload(plist_path: Path) -> None:
    """Unload the agent from launchd (side-effecting; never called from tests)."""
    subprocess.run(["launchctl", "unload", str(plist_path)], check=True)  # pragma: no cover
