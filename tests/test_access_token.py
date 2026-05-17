"""Tests for Settings.access_token: dual-store with AES-GCM-encrypted
QSettings fallback, opportunistic backfill / legacy-plaintext upgrade,
and graceful behavior when the wallet is unusable.

`isolated_settings` (in conftest.py) hands us a Settings instance whose
QSettings backend already lives in test mode. The keyring stand-in
below patches `_keyring_get_token` / `_keyring_set_token` directly,
keeping the real wallet untouched too.

Storage model:
    Primary:    OS keyring (mocked here).
    Resilience: QSettings `server/token` holds an AES-GCM blob with
                a `v1:` version prefix. Plaintext only on legacy
                pre-2026-05-08 installs, detected and upgraded
                forward on first read.
"""

from modules.settings import _ENC_PREFIX, _encrypt_token, _decrypt_token


class _FakeKeyring:
    """Stand-in for the python-keyring backend so tests don't poke the
    user's real wallet."""

    def __init__(self):
        self.store: dict = {}
        self.fail_get = False
        self.fail_set = False

    def get(self, *args, **kwargs):
        if self.fail_get:
            return None
        return self.store.get("token")

    def set(self, value: str) -> bool:
        if self.fail_set:
            return False
        if value:
            self.store["token"] = value
        else:
            self.store.pop("token", None)
        return True


def _patch_keyring(monkeypatch, fake: _FakeKeyring):
    import modules.settings as smod

    monkeypatch.setattr(smod, "_keyring_get_token", fake.get)
    monkeypatch.setattr(smod, "_keyring_set_token", fake.set)


# ── encryption primitives ─────────────────────────────────────────────


def test_encrypt_decrypt_round_trip():
    blob = _encrypt_token("hunter2")
    assert blob.startswith(_ENC_PREFIX)
    # Ciphertext should be different on every call (random nonce).
    assert _encrypt_token("hunter2") != blob
    # But both decrypt to the original plaintext.
    assert _decrypt_token(blob) == "hunter2"


def test_encrypt_empty_returns_empty():
    assert _encrypt_token("") == ""
    assert _decrypt_token("") == ""


def test_decrypt_legacy_plaintext_passes_through():
    # Pre-v1 stored values (no prefix) are returned as-is so the
    # caller can re-encrypt them forward.
    assert _decrypt_token("legacy-plaintext-token") == "legacy-plaintext-token"


def test_decrypt_corrupted_blob_returns_empty():
    # A v1 blob that fails AES-GCM auth (tampered ciphertext, wrong
    # key, …) should not surface gibberish — return empty so callers
    # treat it as "no credential" and fall back to LoginView.
    bad = _ENC_PREFIX + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    assert _decrypt_token(bad) == ""


# ── dual-store contract ───────────────────────────────────────────────


def test_token_round_trip_through_keyring(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "abc123"
    assert isolated_settings.access_token == "abc123"
    assert fake.store == {"token": "abc123"}
    # QSettings holds a *ciphertext* blob with the version prefix,
    # never the plaintext.
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    assert "abc123" not in stored
    # Round-trip through the same machine-key decrypts.
    assert _decrypt_token(stored) == "abc123"


def test_set_writes_both_stores(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.setValue("server/token", "stale")
    isolated_settings.access_token = "fresh"
    assert fake.store == {"token": "fresh"}
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    assert _decrypt_token(stored) == "fresh"


def test_keyring_read_backfills_qsettings(isolated_settings, monkeypatch):
    # Existing install whose QSettings copy was wiped by the prior
    # migrate-and-remove code: backfill on first read so the next
    # boot has the resilience floor in place.
    fake = _FakeKeyring()
    fake.store["token"] = "in-keyring-only"
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.remove("server/token")
    assert isolated_settings.access_token == "in-keyring-only"
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    assert _decrypt_token(stored) == "in-keyring-only"


def test_legacy_plaintext_upgrades_on_read_via_keyring(
    isolated_settings,
    monkeypatch,
):
    # Pre-v1 install: token stored plaintext in QSettings, also in
    # keyring. The legacy plaintext should be replaced by an
    # encrypted blob on first read so we don't continue leaking it.
    fake = _FakeKeyring()
    fake.store["token"] = "legacy-token"
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.setValue("server/token", "legacy-token")
    assert isolated_settings.access_token == "legacy-token"
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    assert _decrypt_token(stored) == "legacy-token"


def test_legacy_plaintext_upgrades_on_read_via_fallback(
    isolated_settings,
    monkeypatch,
):
    # Pre-v1 install with a broken keyring: the fallback path should
    # return the legacy plaintext value AND re-encrypt it forward so
    # subsequent reads no longer have plaintext on disk.
    fake = _FakeKeyring()
    fake.fail_get = True
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.setValue("server/token", "legacy-token")
    assert isolated_settings.access_token == "legacy-token"
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    assert _decrypt_token(stored) == "legacy-token"


def test_keyring_miss_returns_decrypted_qsettings(
    isolated_settings,
    monkeypatch,
):
    # Steady-state for users on systems with no keyring: stored blob
    # is encrypted, read decrypts it, plaintext is never on disk.
    fake = _FakeKeyring()
    fake.fail_get = True
    _patch_keyring(monkeypatch, fake)
    blob = _encrypt_token("encrypted-token")
    isolated_settings._s.setValue("server/token", blob)
    assert isolated_settings.access_token == "encrypted-token"


def test_token_writer_persists_qsettings_when_keyring_broken(
    isolated_settings,
    monkeypatch,
):
    # Keyring write fails (no backend). The QSettings encrypted copy
    # still gets written so the app stays usable.
    fake = _FakeKeyring()
    fake.fail_set = True
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "fallback"
    assert fake.store == {}
    stored = isolated_settings._s.value("server/token", "", type=str)
    assert stored.startswith(_ENC_PREFIX)
    # And reading via the fallback path round-trips correctly.
    fake.fail_get = True
    assert isolated_settings.access_token == "fallback"


def test_clearing_token_purges_both_stores(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "to-clear"
    isolated_settings.access_token = ""
    assert fake.store == {}
    assert isolated_settings._s.value("server/token", "", type=str) == ""
    assert isolated_settings.access_token == ""


def test_logout_path_clears_token(isolated_settings, monkeypatch):
    # Sanity-check that the canonical "logout" pattern — `settings.
    # access_token = ""` — clears both surfaces.
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "active"
    isolated_settings.access_token = ""
    assert isolated_settings.access_token == ""
    assert fake.store == {}
    assert isolated_settings._s.value("server/token", "", type=str) == ""
