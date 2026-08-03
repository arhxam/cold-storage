"""Upgrading from "Save Your Shit" must not look like losing your data.

The project was renamed to Cold Storage after v0.3.1. Three things identify an
existing install — the archive folder, the env var, and the OS keychain entry —
and changing any of them without a fallback would present an existing user with
an empty archive or a permanently locked one.
"""

import base64

import pytest

from coldstorage import paths
from coldstorage.crypto.encryption import KEY_BYTES, Cipher
from coldstorage.crypto.keys import _KEYRING_SERVICE, _LEGACY_KEYRING_SERVICE, KeyManager


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.delenv(paths.LEGACY_ENV_HOME, raising=False)
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(paths, "DEFAULT_HOME", tmp_path / "ColdStorage")
    monkeypatch.setattr(paths, "LEGACY_HOME", tmp_path / "SaveYourShit")
    return tmp_path


def test_existing_old_archive_is_still_found(home):
    legacy = home / "SaveYourShit"
    legacy.mkdir()
    (legacy / "config.toml").write_text("encrypt = true")
    assert paths.get_home() == legacy, "an existing archive must keep being used"


def test_fresh_install_uses_the_new_folder(home):
    assert paths.get_home() == home / "ColdStorage"


def test_new_archive_wins_when_both_exist(home):
    (home / "SaveYourShit").mkdir()
    (home / "SaveYourShit" / "config.toml").write_text("encrypt = true")
    (home / "ColdStorage").mkdir()
    assert paths.get_home() == home / "ColdStorage"


def test_old_env_var_still_works(home, monkeypatch, tmp_path):
    monkeypatch.setenv(paths.LEGACY_ENV_HOME, str(tmp_path / "elsewhere"))
    assert paths.get_home() == (tmp_path / "elsewhere").resolve()


def test_new_env_var_wins_over_old(home, monkeypatch, tmp_path):
    monkeypatch.setenv(paths.LEGACY_ENV_HOME, str(tmp_path / "old"))
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "new"))
    assert paths.get_home() == (tmp_path / "new").resolve()


class _FakeKeyring:
    """Minimal stand-in; the real keychain is never touched by tests."""

    def __init__(self, seed=None):
        self.store = dict(seed or {})

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value


def test_key_cached_under_the_old_name_still_unlocks(tmp_path, monkeypatch):
    """Otherwise the archive reads as locked and we'd demand a Recovery Kit."""
    key = bytes(range(KEY_BYTES))
    fake = _FakeKeyring({(_LEGACY_KEYRING_SERVICE, "master-key"): base64.b64encode(key).decode()})
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
    monkeypatch.delenv("COLD_NO_KEYRING", raising=False)

    km = KeyManager(tmp_path / "keys")
    cipher = km.unlock_from_keychain()
    assert cipher is not None, "the pre-rename cached key must still work"
    blob = cipher.encrypt(b"hello")
    assert Cipher(key).decrypt(blob) == b"hello", "it must be the same key"

    # And it should be copied forward to THIS archive's own per-home account so
    # the shared/legacy fallback is only consulted once.
    assert fake.store.get((_KEYRING_SERVICE, km._keyring_user())) is not None


def test_no_cached_key_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "keyring", _FakeKeyring())
    monkeypatch.delenv("COLD_NO_KEYRING", raising=False)
    assert KeyManager(tmp_path / "keys").unlock_from_keychain() is None


def test_two_archives_never_share_a_keychain_slot(tmp_path, monkeypatch):
    """A second archive's cached key must NOT overwrite the first's.

    This is the regression guard for the shared-account bug that once left an
    archive's media undecryptable after a second init on the same machine.
    """
    monkeypatch.setenv("COLD_SCRYPT_N", "16384")  # keep the KDF fast in tests
    monkeypatch.setitem(__import__("sys").modules, "keyring", _FakeKeyring())
    monkeypatch.delenv("COLD_NO_KEYRING", raising=False)

    a = KeyManager(tmp_path / "archive-a" / "keys")
    b = KeyManager(tmp_path / "archive-b" / "keys")
    cipher_a, _ = a.create("passphrase-a")
    a.cache_in_keychain(cipher_a)
    cipher_b, _ = b.create("passphrase-b")  # a second init — the old bug's trigger
    b.cache_in_keychain(cipher_b)

    assert cipher_a._key != cipher_b._key  # noqa: SLF001
    assert a._keyring_user() != b._keyring_user()
    # Each archive still unlocks to its OWN key — no clobbering.
    assert a.unlock_from_keychain()._key == cipher_a._key  # noqa: SLF001
    assert b.unlock_from_keychain()._key == cipher_b._key  # noqa: SLF001


def test_app_migrates_the_old_electron_user_data():
    """The Electron migration moves Partitions/, which holds every sign-in."""
    from pathlib import Path

    src = Path("app/main.js").read_text()
    assert "migrateLegacyUserData" in src
    assert '"Save Your Shit"' in src, "must know the old product name"
    assert "Partitions" in src
    # It has to run before whenReady, or the session layer reads the new path
    # first. Match the actual call site, not the mention in the comment above it.
    assert src.index("migrateLegacyUserData();") < src.index("app.whenReady().then")


def test_app_also_renames_the_session_partitions():
    """Copying userData across is not enough on its own.

    The partition names changed with the project (persist:syt-<id> ->
    persist:cold-<id>), so a migrated directory still leaves every sign-in in a
    folder the new code never opens — the user would silently have to reconnect
    every account. Caught by running the real upgrade rather than assuming.
    """
    from pathlib import Path

    src = Path("app/main.js").read_text()
    assert "migrateLegacyPartitions" in src
    assert '"syt-"' in src, "must recognise the old partition prefix"
    assert '"cold-" + name.slice(4)' in src, "must map it onto the new one"
    # It must keep running after the first launch: by then Partitions/ exists
    # and the directory copy is skipped, but a stale syt-* may still be there.
    body = src[src.index("function migrateLegacyUserData"):]
    body = body[: body.index("\nfunction migrateLegacyPartitions")]
    assert body.rstrip().count("migrateLegacyPartitions(current);") == 1
    assert "if (fs.existsSync(path.join(current, \"Partitions\"))) return;" not in body, (
        "an early return before the partition rename would skip it on every "
        "launch after the first"
    )
