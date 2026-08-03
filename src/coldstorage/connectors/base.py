"""Connector contract + registry.

A connector is one small module per platform. In Phase 1 the primary rail is
**export ingest**: the user downloads their official "Download your data" archive
and points us at it; the connector parses that archive into normalized batches.

Live API/session rails (Telegram, Reddit, instaloader) implement :meth:`run` in
later phases; they share the same normalization output so the rest of the system
does not care how the data was obtained.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from ..models import Batch
from ..textutil import fix_meta_mojibake_deep


class Connector(ABC):
    id: str = ""
    display_name: str = ""
    rail: str = "export"  # export | api | session
    provides: list[str] = []
    #: True for Rail C connectors that carry account-ban risk (opt-in only).
    risky: bool = False

    @abstractmethod
    def detect(self, path: Path) -> bool:
        """Return True if ``path`` looks like this platform's export."""

    @abstractmethod
    def parse_export(self, path: Path) -> Iterator[Batch]:
        """Parse an unpacked export directory into batches of normalized records."""


# -- shared helpers ---------------------------------------------------------

def load_json(path: Path, *, fix_mojibake: bool = False) -> object:
    """Load a JSON file, optionally repairing Meta's double-encoded text."""
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    return fix_meta_mojibake_deep(data) if fix_mojibake else data


def ensure_unpacked(path: Path, dest: Path) -> Path:
    """If ``path`` is an archive, extract it into ``dest`` and return the dir.

    Handles ``.zip`` (Instagram, Facebook, X, Discord, Snapchat, LinkedIn,
    Reddit, Slack, WhatsApp) and ``.tar.gz``/``.tgz``/``.tar`` (Google Takeout
    can deliver either). Otherwise returns ``path`` unchanged. Guards against
    zip-slip / tar path traversal.
    """
    path = Path(path)
    if path.is_dir():
        return path

    if zipfile.is_zipfile(path):
        dest.mkdir(parents=True, exist_ok=True)
        dest_root = str(dest.resolve())
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if not str(target).startswith(dest_root):
                    raise ValueError(f"unsafe path in zip: {member}")
            zf.extractall(dest)
        return dest

    # Google Takeout (and some others) can be a gzipped/plain tarball. is_tarfile
    # sniffs the bytes, so a .tgz named without a suffix is still recognized.
    if tarfile.is_tarfile(path):
        dest.mkdir(parents=True, exist_ok=True)
        dest_root = str(dest.resolve())
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                target = (dest / member.name).resolve()
                if not str(target).startswith(dest_root):
                    raise ValueError(f"unsafe path in tar: {member.name}")
            # `filter="data"` (3.12+) blocks absolute paths, device files and
            # unsafe links — belt-and-suspenders with the check above.
            try:
                tf.extractall(dest, filter="data")
            except TypeError:  # older Pythons without the filter kwarg
                tf.extractall(dest)
        return dest

    return path


def stable_uid(prefix: str, *parts: object, seen: dict[str, int] | None = None) -> str:
    """A dedup key derived from a record's content, never its position.

    Using a list index means an export with one new item prepended — which is
    how Instagram, Snapchat and Google Takeout all deliver updates — shifts
    every later item onto a different uid. The archive then stores a second
    copy of everything old, and the genuinely new item lands on a uid that
    already exists and is dropped as a duplicate. The user is told it worked.

    Identical-looking records do occur (three photos posted in one second, with
    no caption). Pass ``seen`` to give the second and later occurrences a
    stable suffix, so they stay distinct without reintroducing position.
    """
    body = "|".join("" if p is None else str(p) for p in parts)
    h = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:20]
    if seen is not None:
        n = seen.get(h, 0)
        seen[h] = n + 1
        if n:
            return f"{prefix}:{h}:{n}"
    return f"{prefix}:{h}"


# -- registry ---------------------------------------------------------------

_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _REGISTRY[connector.id] = connector
    return connector


def get(connector_id: str) -> Connector:
    if connector_id not in _REGISTRY:
        raise KeyError(f"unknown connector: {connector_id}")
    return _REGISTRY[connector_id]


def all_connectors() -> list[Connector]:
    return list(_REGISTRY.values())


def detect_connector(path: Path) -> Connector | None:
    """Return the first registered connector that recognizes ``path``."""
    for c in _REGISTRY.values():
        try:
            if c.detect(Path(path)):
                return c
        except Exception:
            continue
    return None
