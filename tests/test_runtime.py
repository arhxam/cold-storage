import pytest

from saveyourshit.config import Config
from saveyourshit.crypto import KeyManager
from saveyourshit.paths import Layout
from saveyourshit.runtime import LockedError, resolve_cipher


def test_no_encrypt_returns_no_cipher(home):
    layout = Layout(home).ensure()
    cfg = Config(encrypt=False)
    assert resolve_cipher(cfg, layout) is None


def test_encrypted_requires_keyfile(home):
    layout = Layout(home).ensure()
    cfg = Config(encrypt=True)
    with pytest.raises(LockedError):
        resolve_cipher(cfg, layout)


def test_encrypted_unlocks_with_passphrase(home):
    layout = Layout(home).ensure()
    KeyManager(layout.keys_dir).create("pw")
    cfg = Config(encrypt=True)
    cipher = resolve_cipher(cfg, layout, passphrase="pw")
    assert cipher is not None
    blob = cipher.encrypt(b"x")
    assert cipher.decrypt(blob) == b"x"


def test_encrypted_unlocks_from_env(home, monkeypatch):
    layout = Layout(home).ensure()
    KeyManager(layout.keys_dir).create("envpass")
    monkeypatch.setenv("SYT_PASSPHRASE", "envpass")
    cfg = Config(encrypt=True)
    assert resolve_cipher(cfg, layout) is not None


def test_locked_when_no_passphrase_available(home, monkeypatch):
    layout = Layout(home).ensure()
    KeyManager(layout.keys_dir).create("pw")
    monkeypatch.delenv("SYT_PASSPHRASE", raising=False)
    # keychain disabled by the autouse fixture; stdin is not a TTY under pytest
    cfg = Config(encrypt=True)
    with pytest.raises(LockedError):
        resolve_cipher(cfg, layout)
