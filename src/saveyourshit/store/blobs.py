"""Content-addressed blob store with optional at-rest encryption.

Media is keyed by the SHA-256 of its **plaintext**, so identical files across
posts and re-exports are stored exactly once (dedup) regardless of whether
encryption (which uses a random nonce each time) is on. Encrypted blobs are
written with a ``.enc`` suffix so the store can tell the two apart.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..crypto import Cipher


class BlobStore:
    def __init__(self, root: Path, cipher: Cipher | None = None) -> None:
        self.root = Path(root)
        self.cipher = cipher

    def _shard_path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / sha

    def _stored_path(self, sha: str) -> Path:
        base = self._shard_path(sha)
        return base.with_name(base.name + ".enc") if self.cipher else base

    def has(self, sha: str) -> bool:
        return self._stored_path(sha).exists()

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self._stored_path(sha)
        if dest.exists():
            return sha  # dedup: already stored
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = self.cipher.encrypt(data) if self.cipher else data
        tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}")
        tmp.write_bytes(payload)
        tmp.replace(dest)
        return sha

    def put_file(self, path: Path) -> str:
        return self.put_bytes(Path(path).read_bytes())

    def get_bytes(self, sha: str) -> bytes:
        dest = self._stored_path(sha)
        if not dest.exists():
            raise FileNotFoundError(f"blob {sha} not found")
        raw = dest.read_bytes()
        return self.cipher.decrypt(raw) if self.cipher else raw

    def verify(self, sha: str) -> bool:
        """Return True iff the stored blob decrypts and matches its content hash."""
        try:
            data = self.get_bytes(sha)
        except Exception:
            return False
        return hashlib.sha256(data).hexdigest() == sha

    def count(self) -> int:
        if not self.root.exists():
            return 0
        return sum(1 for p in self.root.rglob("*") if p.is_file())

    def total_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
