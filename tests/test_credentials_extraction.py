"""Pin the credentials.py extraction's re-export contract.

The credential crypto + dual-store keyring functions moved from
settings.py to modules.credentials (2026-06-02). External callers
(modules.airplay2, the jellytoast boot warm-up) and the access-token /
airplay-credential tests import them from modules.settings, which
re-imports them. These tests pin that re-export so a future "unused
import" cleanup can't silently break those callers + their monkeypatches.
"""

import modules.credentials as cred
import modules.settings as settings

_NAMES = [
    "_ENC_PREFIX",
    "_KEYRING_SERVICE",
    "_KEYRING_USERNAME",
    "_LEGACY_KEYRING_SERVICE",
    "_machine_key",
    "_encrypt_token",
    "_decrypt_token",
    "warm_keyring_async",
    "_keyring_get_token",
    "_keyring_set_token",
]


def test_credentials_module_defines_all_names():
    for n in _NAMES:
        assert hasattr(cred, n), f"credentials.py is missing {n}"


def test_settings_reexports_every_credential_name():
    for n in _NAMES:
        assert hasattr(settings, n), f"settings no longer re-exports {n}"
        # Re-imported straight from credentials → the same object, so a
        # bare call in settings (and a monkeypatch on settings) both hit it.
        assert getattr(settings, n) is getattr(cred, n)


def test_round_trip_via_credentials_module():
    blob = cred._encrypt_token("s3cret")
    assert blob.startswith(cred._ENC_PREFIX)
    assert cred._decrypt_token(blob) == "s3cret"


def test_legacy_plaintext_passes_through():
    # A value without the version prefix is treated as legacy plaintext.
    assert cred._decrypt_token("legacy-plain") == "legacy-plain"


def test_empty_inputs():
    assert cred._encrypt_token("") == ""
    assert cred._decrypt_token("") == ""
