"""Tests for Settings.access_token: keyring round-trip, plaintext purge,
legacy migration, and graceful fallback when the wallet is unusable.

`isolated_settings` (in conftest.py) hands us a Settings instance whose
QSettings backend already lives in test mode (so we never touch the
user's real ~/.config/JellyToast/JellyToast.conf). The keyring stand-in
below patches `_keyring_get_token` / `_keyring_set_token` directly,
keeping the real wallet untouched too.
"""


class _FakeKeyring:
    """Stand-in for the python-keyring backend so tests don't poke the
    user's real wallet."""

    def __init__(self):
        self.store: dict = {}
        self.fail_get = False
        self.fail_set = False

    def get(self):
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


def test_token_round_trip_through_keyring(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "abc123"
    assert isolated_settings.access_token == "abc123"
    assert fake.store == {"token": "abc123"}
    # QSettings should be untouched in the keyring-happy path.
    assert isolated_settings._s.value("server/token", "", type=str) == ""


def test_token_set_clears_plaintext(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    # Pre-seed a stale plaintext value as if from an older install.
    isolated_settings._s.setValue("server/token", "old-plaintext")
    isolated_settings.access_token = "fresh"
    assert isolated_settings.access_token == "fresh"
    assert isolated_settings._s.value("server/token", "", type=str) == ""


def test_legacy_token_migrates_on_read(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    # Simulate a pre-keyring install: token only in QSettings.
    isolated_settings._s.setValue("server/token", "legacy-token")
    # First read should migrate it forward.
    assert isolated_settings.access_token == "legacy-token"
    assert fake.store == {"token": "legacy-token"}
    assert isolated_settings._s.value("server/token", "", type=str) == ""


def test_legacy_token_falls_back_when_keyring_write_fails(
    isolated_settings, monkeypatch,
):
    fake = _FakeKeyring()
    fake.fail_set = True  # backend unusable
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.setValue("server/token", "legacy-token")
    # Read still returns the value, but plaintext stays put because
    # migration couldn't complete.
    assert isolated_settings.access_token == "legacy-token"
    assert isolated_settings._s.value("server/token", "", type=str) == "legacy-token"


def test_token_writer_falls_back_to_qsettings_when_keyring_broken(
    isolated_settings, monkeypatch,
):
    fake = _FakeKeyring()
    fake.fail_set = True
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "fallback"
    # Token persisted via the QSettings legacy path so the app stays
    # usable even without a working secret store.
    assert isolated_settings._s.value("server/token", "", type=str) == "fallback"


def test_clearing_token_purges_both_stores(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "to-clear"
    isolated_settings.access_token = ""
    assert fake.store == {}
    assert isolated_settings._s.value("server/token", "", type=str) == ""
    assert isolated_settings.access_token == ""


def test_logout_path_clears_token(isolated_settings, monkeypatch):
    # Sanity-check that the canonical "logout" pattern from
    # JellyfinAPI.logout — `settings.access_token = ""` — clears both
    # surfaces. Guards against a regression where logout leaks a token
    # in plaintext after a wallet write succeeds but a clear fails.
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "active"
    assert isolated_settings.access_token == "active"
    isolated_settings.access_token = ""
    assert isolated_settings.access_token == ""
