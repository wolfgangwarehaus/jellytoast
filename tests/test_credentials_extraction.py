"""Pin the credentials.py extraction's re-export contract.

The credential crypto + dual-store keyring functions moved from
settings.py to jellytoast.credentials (2026-06-02). External callers
(jellytoast.airplay2, the jellytoast boot warm-up) and the access-token /
airplay-credential tests import them from jellytoast.settings, which
re-imports them. These tests pin that re-export so a future "unused
import" cleanup can't silently break those callers + their monkeypatches.
"""

import jellytoast.credentials as cred
import jellytoast.settings as settings

# Everything credentials.py owns.
_ALL = [
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

# The subset settings re-exports — what external callers reach via
# jellytoast.settings (airplay2 + the boot warm-up + the access-token /
# airplay tests, including their monkeypatches of _keyring_get/set_token).
# The bare _KEYRING_* constants are NOT re-exported: only the now-extracted
# settings_migration used them, and it imports them straight from
# credentials. They stay credentials-owned.
_SETTINGS_REEXPORTS = [
    "_ENC_PREFIX",
    "_machine_key",
    "_encrypt_token",
    "_decrypt_token",
    "warm_keyring_async",
    "_keyring_get_token",
    "_keyring_set_token",
]


def test_credentials_module_defines_all_names():
    for n in _ALL:
        assert hasattr(cred, n), f"credentials.py is missing {n}"


def test_settings_reexports_the_external_contract():
    for n in _SETTINGS_REEXPORTS:
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


# ── Windows DPAPI resilience path + v1→"" migration safety ──────────────────
# On Windows the app ships no cryptography (no win_arm64 wheel), so the AES-GCM
# resilience store is replaced by DPAPI. These run on any OS by patching
# IS_WINDOWS + a fake DPAPI layer, so CI (Linux) exercises the Windows branch.


def _fake_dpapi():
    """A reversible stand-in for CryptProtectData/CryptUnprotectData. Prepends
    a marker incl. a NUL byte so the binary-safe (embedded-NUL) path is tested."""

    def protect(pt: bytes) -> bytes:
        return b"D\x00P" + pt

    def unprotect(ct: bytes) -> bytes:
        assert ct.startswith(b"D\x00P")
        return ct[3:]

    return protect, unprotect


def _as_windows(monkeypatch):
    monkeypatch.setattr(cred, "IS_WINDOWS", True)
    monkeypatch.setattr(cred, "_dpapi_fns", _fake_dpapi(), raising=False)


def test_windows_dpapi_round_trip(monkeypatch):
    _as_windows(monkeypatch)
    blob = cred._encrypt_token("win-token-xyz")
    assert blob.startswith(cred._ENC_PREFIX_DPAPI)  # "d1:"
    assert cred._decrypt_token(blob) == "win-token-xyz"


def test_windows_dpapi_binary_safe(monkeypatch):
    # A token whose ciphertext carries embedded NULs must survive — guards the
    # POINTER(c_char) vs c_char_p truncation trap in the real ctypes binding.
    _as_windows(monkeypatch)
    secret = "\x00\x01token\x00end"
    assert cred._decrypt_token(cred._encrypt_token(secret)) == secret


def test_v1_blob_on_windows_degrades_to_empty(monkeypatch):
    # THE migration-safety guard: a legacy "v1:" AES-GCM blob can't be decrypted
    # on Windows (cryptography absent). It MUST decrypt to "" — never be handed
    # back raw, which the caller's re-encrypt-forward path would corrupt into a
    # bogus DPAPI blob, destroying the original token unrecoverably.
    import base64

    _as_windows(monkeypatch)
    v1 = "v1:" + base64.b64encode(b"old-aesgcm-ciphertext-bytes").decode("ascii")
    assert cred._decrypt_token(v1) == ""


def test_d1_blob_off_windows_degrades_to_empty(monkeypatch):
    # Symmetric: a DPAPI "d1:" blob on a non-Windows box is unreadable → "".
    monkeypatch.setattr(cred, "IS_WINDOWS", False)
    assert cred._decrypt_token("d1:AAAAstuff") == ""


def test_legacy_plaintext_passes_through_on_windows(monkeypatch):
    # A genuinely unprefixed value is still legacy plaintext on Windows too.
    _as_windows(monkeypatch)
    assert cred._decrypt_token("bare-legacy-plaintext") == "bare-legacy-plaintext"


def test_enc_prefix_tracks_platform(monkeypatch):
    # The "current format" prefix the self-heal guards test against is d1 on
    # Windows, v1 elsewhere; both are recognised as ciphertext.
    assert cred._ENC_PREFIX_AESGCM == "v1:"
    assert cred._ENC_PREFIX_DPAPI == "d1:"
    assert set(cred._KNOWN_PREFIXES) == {"v1:", "d1:"}


def test_keyring_backend_error_warns_once(monkeypatch, caplog):
    # No-backend box: two boot reads must surface a single concise INFO, not a
    # verbose WARNING per read (boot-log noise cleanup, 2026-06-05).
    import keyring

    monkeypatch.setattr(cred, "_KEYRING_WARNED", False, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr(keyring, "get_password", _boom)
    with caplog.at_level("INFO", logger="jellytoast.credentials"):
        assert cred._keyring_get_token(max_attempts=1, interval_s=0) is None
        assert cred._keyring_get_token(max_attempts=1, interval_s=0) is None

    hits = [r for r in caplog.records if "OS keyring unavailable" in r.message]
    assert len(hits) == 1
    assert hits[0].levelname == "INFO"
