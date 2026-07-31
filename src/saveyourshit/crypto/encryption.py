"""Authenticated encryption (AES-256-GCM) for data at rest.

A :class:`Cipher` is built from a 32-byte master key. Each ``encrypt`` call uses a
fresh random 96-bit nonce, prepended to the ciphertext. Format:

    blob = nonce(12) || ciphertext_with_tag
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_BYTES = 32


def derive_key(passphrase: str, salt: bytes, *, n: int = 2**15, r: int = 8, p: int = 1) -> bytes:
    """Derive a 32-byte key from a passphrase using scrypt (memory-hard).

    ``maxmem`` must be raised above OpenSSL's ~32 MiB default because scrypt at
    these parameters needs ``128 * n * r`` bytes (~33 MiB).
    """
    maxmem = 128 * n * r * 2
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=maxmem
    )


class Cipher:
    """Symmetric AES-256-GCM cipher over a 32-byte master key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
        self._key = key
        self._aead = AESGCM(key)

    def encrypt(self, data: bytes, *, aad: bytes | None = None) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, data, aad)

    def decrypt(self, blob: bytes, *, aad: bytes | None = None) -> bytes:
        if len(blob) < NONCE_BYTES:
            raise ValueError("ciphertext too short")
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        return self._aead.decrypt(nonce, ct, aad)

    def wrap(self, other_key: bytes) -> bytes:
        """Encrypt another key with this one (key-wrapping)."""
        return self.encrypt(other_key)

    def unwrap(self, wrapped: bytes) -> bytes:
        return self.decrypt(wrapped)
