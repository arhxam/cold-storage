import pytest

from saveyourshit.crypto import KeyManager, RecoveryKit
from saveyourshit.crypto.keys import KeyError_


def test_encrypt_decrypt_roundtrip(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, _ = km.create("hunter2")
    blob = cipher.encrypt(b"private dm content")
    assert blob != b"private dm content"
    assert cipher.decrypt(blob) == b"private dm content"


def test_unlock_with_correct_passphrase(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, _ = km.create("correct horse")
    secret = cipher.encrypt(b"x")
    assert km.unlock("correct horse").decrypt(secret) == b"x"


def test_wrong_passphrase_rejected(tmp_path):
    km = KeyManager(tmp_path / "keys")
    km.create("right")
    with pytest.raises(KeyError_):
        km.unlock("wrong")


def test_recovery_kit_roundtrip(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, kit = km.create("pw")
    secret = cipher.encrypt(b"y")
    recovered = km.unlock_with_recovery(RecoveryKit(kit.code))
    assert recovered.decrypt(secret) == b"y"


def test_recovery_kit_detects_typos(tmp_path):
    km = KeyManager(tmp_path / "keys")
    _, kit = km.create("pw")
    # corrupt one character in the body
    bad = list(kit.code)
    bad[0] = "A" if bad[0] != "A" else "B"
    with pytest.raises(KeyError_):
        RecoveryKit("".join(bad)).to_key()


def test_change_passphrase_preserves_data(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, _ = km.create("old")
    blob = cipher.encrypt(b"z")
    km.change_passphrase("old", "new")
    assert km.unlock("new").decrypt(blob) == b"z"
    with pytest.raises(KeyError_):
        km.unlock("old")


def test_reset_passphrase_with_recovery(tmp_path):
    km = KeyManager(tmp_path / "keys")
    cipher, kit = km.create("forgotten")
    blob = cipher.encrypt(b"w")
    km.reset_passphrase_with_recovery(RecoveryKit(kit.code), "brandnew")
    assert km.unlock("brandnew").decrypt(blob) == b"w"


def test_cannot_create_twice(tmp_path):
    km = KeyManager(tmp_path / "keys")
    km.create("a")
    with pytest.raises(KeyError_):
        km.create("b")
