"""Filesystem layout. The user always knows exactly where their data lives.

Default home is ``~/ColdStorage``. Override with the ``COLD_HOME`` environment
variable (used heavily in tests). Everything — config, index, blobs, per-connector
folders — lives under one home directory so it is trivially portable and
back-up-able as a single tree.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "COLD_HOME"
DEFAULT_HOME = Path.home() / "ColdStorage"

#: This project was called "Save Your Shit" up to v0.3.1. Someone upgrading has
#: an archive at the old path and possibly the old env var set, and silently
#: pointing them at a new empty folder would look exactly like losing all of
#: their data. Both are still honoured, and the old archive is preferred when
#: it is the only one that exists.
LEGACY_ENV_HOME = "SYT_HOME"
LEGACY_HOME = Path.home() / "SaveYourShit"


def get_home() -> Path:
    """Return the Cold Storage home directory (not guaranteed to exist yet)."""
    override = os.environ.get(ENV_HOME) or os.environ.get(LEGACY_ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    # Keep using an existing pre-rename archive rather than starting an empty one.
    if not DEFAULT_HOME.exists() and (LEGACY_HOME / "config.toml").exists():
        return LEGACY_HOME
    return DEFAULT_HOME


class Layout:
    """Resolves and (optionally) creates the standard directory layout."""

    def __init__(self, home: Path | str | None = None) -> None:
        # Accept a str: callers get this path from env vars and CLI args, and
        # AttributeError on a plain string is a pointlessly sharp edge.
        self.home = Path(home).resolve() if home else get_home().resolve()

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def index_db(self) -> Path:
        return self.home / "index.sqlite"

    @property
    def blobs_dir(self) -> Path:
        return self.home / "blobs"

    @property
    def runs_dir(self) -> Path:
        return self.home / ".runs"

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    def connector_dir(self, connector: str) -> Path:
        return self.home / connector

    def snapshots_dir(self, connector: str) -> Path:
        return self.connector_dir(connector) / "snapshots"

    def manifest_file(self, connector: str) -> Path:
        return self.connector_dir(connector) / "manifest.jsonl"

    def ensure(self) -> Layout:
        """Create the base directories. Idempotent."""
        for d in (self.home, self.blobs_dir, self.runs_dir, self.keys_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.config_file.exists()
