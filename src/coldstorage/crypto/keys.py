"""Key custody: master key, passphrase wrapping, and the Recovery Kit.

Design:

* A random 32-byte **master key** encrypts all data (via :class:`Cipher`).
* The master key is **wrapped** (encrypted) with a key derived from the user's
  passphrase, and stored in ``keys/keyfile.json``. Changing the passphrase only
  re-wraps the master — it never re-encrypts the archive.
* The master key is also emitted once as a **Recovery Kit** (a base32 code the
  user prints/saves). If they forget the passphrase *and* lose the OS keychain,
  the Recovery Kit is the only way back in — by design there is no reset link.
* Optionally the master key is cached in the OS keychain so scheduled backups run
  without a passphrase prompt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .encryption import KEY_BYTES, Cipher, derive_key

KEYFILE_VERSION = 1
_KEYRING_SERVICE = "coldstorage"
#: The pre-rename service name (this project was "Save Your Shit" up to v0.3.1).
#: Read-only fallback so an existing cached key keeps working after upgrading.
_LEGACY_KEYRING_SERVICE = "saveyourshit"
_KEYRING_USER = "master-key"


class KeyError_(Exception):
    """Raised on wrong passphrase, corrupt keyfile, or invalid recovery code."""


@dataclass
class RecoveryKit:
    """A human-savable representation of the master key."""

    code: str  # grouped base32, e.g. "ABCD-EFGH-...-1234"

    @classmethod
    def from_key(cls, key: bytes) -> RecoveryKit:
        b32 = base64.b32encode(key).decode("ascii").rstrip("=")
        checksum = base64.b32encode(hashlib.sha256(key).digest()[:2]).decode("ascii").rstrip("=")
        raw = b32 + checksum
        groups = [raw[i : i + 4] for i in range(0, len(raw), 4)]
        return cls(code="-".join(groups))

    def to_key(self) -> bytes:
        raw = self.code.replace("-", "").replace(" ", "").upper()
        # last 4 base32 chars encode the 2-byte checksum
        body, check = raw[:-4], raw[-4:]
        key = base64.b32decode(body + "=" * (-len(body) % 8))
        if len(key) != KEY_BYTES:
            raise KeyError_("recovery code has the wrong length")
        expect = base64.b32encode(hashlib.sha256(key).digest()[:2]).decode("ascii").rstrip("=")
        if check.rstrip("=") != expect:
            raise KeyError_("recovery code checksum mismatch — check for typos")
        return key


class KeyManager:
    """Manages the keyfile and hands out a :class:`Cipher`."""

    def __init__(self, keys_dir: Path) -> None:
        self.keys_dir = Path(keys_dir)
        self.keyfile = self.keys_dir / "keyfile.json"

    def exists(self) -> bool:
        return self.keyfile.exists()

    # -- creation ------------------------------------------------------------
    def create(self, passphrase: str) -> tuple[Cipher, RecoveryKit]:
        """Initialize a brand-new master key. Returns cipher + recovery kit."""
        if self.exists():
            raise KeyError_("keyfile already exists; refusing to overwrite")
        master = os.urandom(KEY_BYTES)
        self._write_keyfile(master, passphrase)
        return Cipher(master), RecoveryKit.from_key(master)

    # -- unlocking -----------------------------------------------------------
    def unlock(self, passphrase: str) -> Cipher:
        master = self._unwrap_master(passphrase)
        return Cipher(master)

    def unlock_with_recovery(self, kit: RecoveryKit) -> Cipher:
        stored = self._load_keyfile()
        master = kit.to_key()
        # sanity check: the recovery key must match what the keyfile wraps
        expect = bytes.fromhex(stored["master_fingerprint"])
        if hashlib.sha256(master).digest()[:8] != expect:
            raise KeyError_("recovery code does not match this archive")
        return Cipher(master)

    def change_passphrase(self, old: str, new: str) -> None:
        master = self._unwrap_master(old)
        self._write_keyfile(master, new)

    def reset_passphrase_with_recovery(self, kit: RecoveryKit, new_passphrase: str) -> Cipher:
        cipher = self.unlock_with_recovery(kit)
        self._write_keyfile(kit.to_key(), new_passphrase)
        return cipher

    # -- OS keychain cache ---------------------------------------------------
    def cache_in_keychain(self, cipher: Cipher) -> bool:
        if os.environ.get("COLD_NO_KEYRING"):
            return False
        try:
            import keyring
        except ImportError:
            return False
        try:
            master = cipher._key  # noqa: SLF001 — intentional internal access
            keyring.set_password(
                _KEYRING_SERVICE, _KEYRING_USER, base64.b64encode(master).decode()
            )
            return True
        except Exception:
            return False  # no usable keychain backend (e.g. headless Linux)

    def unlock_from_keychain(self) -> Cipher | None:
        if os.environ.get("COLD_NO_KEYRING"):
            return None
        try:
            import keyring
        except ImportError:
            return None
        stored = None
        try:
            stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            return None
        if not stored:
            # Pre-rename installs cached the key under the old service name.
            # Without this the archive would look permanently locked and the
            # user would be told to dig out their Recovery Kit for no reason.
            try:
                stored = keyring.get_password(_LEGACY_KEYRING_SERVICE, _KEYRING_USER)
            except Exception:
                return None
            if not stored:
                return None
            # Re-cache under the new name so this lookup only happens once.
            # Reading worked; failing to write it forward is not fatal.
            with suppress(Exception):
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, stored)
        return Cipher(base64.b64decode(stored))

    # -- internals -----------------------------------------------------------
    def _scrypt_n(self) -> int:
        """scrypt cost, overridable via ``COLD_SCRYPT_N`` (used to speed up tests)."""
        override = os.environ.get("COLD_SCRYPT_N")
        return int(override) if override else 2**15

    def _write_keyfile(self, master: bytes, passphrase: str) -> None:
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        salt = os.urandom(16)
        n, r, p = self._scrypt_n(), 8, 1
        kek = Cipher(derive_key(passphrase, salt, n=n, r=r, p=p))
        data = {
            "version": KEYFILE_VERSION,
            "kdf": "scrypt",
            "n": n,
            "r": r,
            "p": p,
            "salt": base64.b64encode(salt).decode(),
            "wrapped_master": base64.b64encode(kek.wrap(master)).decode(),
            "master_fingerprint": hashlib.sha256(master).digest()[:8].hex(),
        }
        tmp = self.keyfile.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.keyfile)

    def _load_keyfile(self) -> dict:
        if not self.exists():
            raise KeyError_("no keyfile; run `cold init` first")
        return json.loads(self.keyfile.read_text(encoding="utf-8"))

    def _unwrap_master(self, passphrase: str) -> bytes:
        data = self._load_keyfile()
        salt = base64.b64decode(data["salt"])
        # Use the params stored in the keyfile so archives stay unlockable even if
        # the default cost changes in a future version.
        n = int(data.get("n", 2**15))
        r = int(data.get("r", 8))
        p = int(data.get("p", 1))
        kek = Cipher(derive_key(passphrase, salt, n=n, r=r, p=p))
        try:
            return kek.unwrap(base64.b64decode(data["wrapped_master"]))
        except Exception as exc:  # cryptography raises InvalidTag on bad passphrase
            raise KeyError_("wrong passphrase") from exc
