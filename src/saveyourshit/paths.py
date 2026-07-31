"""Filesystem layout. The user always knows exactly where their data lives.

Default home is ``~/SaveYourShit``. Override with the ``SYT_HOME`` environment
variable (used heavily in tests). Everything — config, index, blobs, per-connector
folders — lives under one home directory so it is trivially portable and
back-up-able as a single tree.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "SYT_HOME"
DEFAULT_HOME = Path.home() / "SaveYourShit"


def get_home() -> Path:
    """Return the Save Your Shit home directory (not guaranteed to exist yet)."""
    override = os.environ.get(ENV_HOME)
    return Path(override).expanduser().resolve() if override else DEFAULT_HOME


class Layout:
    """Resolves and (optionally) creates the standard directory layout."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or get_home()).resolve()

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
