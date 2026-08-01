"""Helpers to open the archive with the right encryption cipher.

Passphrase resolution order (first hit wins), so scheduled/headless runs never
block on a prompt:

1. the OS keychain cache (set at ``init`` time),
2. the ``COLD_PASSPHRASE`` environment variable,
3. an interactive prompt (only when attached to a TTY).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .config import Config
from .crypto import Cipher, KeyManager
from .crypto.keys import KeyError_
from .paths import Layout
from .store.archive import Archive


class LockedError(Exception):
    """Raised when the archive is encrypted but no passphrase is available."""


@dataclass
class Runtime:
    config: Config
    layout: Layout
    cipher: Cipher | None

    def open_archive(self) -> Archive:
        return Archive(self.layout, cipher=self.cipher)


def resolve_cipher(
    config: Config, layout: Layout, *, passphrase: str | None = None
) -> Cipher | None:
    if not config.encrypt:
        return None
    km = KeyManager(layout.keys_dir)
    if not km.exists():
        raise LockedError("no keyfile found — run `cold init` first")

    if passphrase:
        return km.unlock(passphrase)

    cached = km.unlock_from_keychain()
    if cached is not None:
        return cached

    env_pass = os.environ.get("COLD_PASSPHRASE")
    if env_pass:
        return km.unlock(env_pass)

    if sys.stdin.isatty():
        import getpass

        for _ in range(3):
            try:
                return km.unlock(getpass.getpass("Passphrase: "))
            except KeyError_:
                print("Wrong passphrase, try again.")
        raise LockedError("could not unlock archive")

    raise LockedError(
        "archive is encrypted and locked. Set COLD_PASSPHRASE or run interactively."
    )


def load_runtime(home=None, *, passphrase: str | None = None) -> Runtime:
    layout = Layout(home)
    config = Config.load(layout)
    cipher = resolve_cipher(config, layout, passphrase=passphrase)
    return Runtime(config=config, layout=layout, cipher=cipher)
