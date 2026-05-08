"""Tests for Settings.access_token: dual-store round-trip (keyring +
QSettings), opportunistic backfill, and graceful fallback when the
wallet is unusable.

`isolated_settings` (in conftest.py) hands us a Settings instance whose
QSettings backend already lives in test mode (so we never touch the
user's real ~/.config/JellyToast/JellyToast.conf). The keyring stand-in
below patches `_keyring_get_token` / `_keyring_set_token` directly,
keeping the real wallet untouched too.

The current model is a deliberate dual-store: the token lives in BOTH
keyring (preferred — encrypted, OS-managed) AND QSettings (resilience
floor — instantly available, owner-only on ~/.config). On read we
prefer the keyring; on miss we fall back to QSettings instantly.
Both stores are written on every set so they don't drift.
"""


class _FakeKeyring:
    """Stand-in for the python-keyring backend so tests don't poke the
    user's real wallet."""

    def __init__(self):
        self.store: dict = {}
        self.fail_get = False
        self.fail_set = False

    def get(self, *args, **kwargs):
        # Absorbs `_keyring_get_token`'s retry kwargs (max_attempts /
        # interval_s) without caring about them — tests don't simulate
        # the slow-keyring race; that's exactly what the QSettings
        # fallback exists to absorb.
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
    # QSettings should also hold the token — the dual-store is what
    # keeps boot resilient when the keyring is sleepy.
    assert isolated_settings._s.value("server/token", "", type=str) == "abc123"


def test_set_writes_both_stores(isolated_settings, monkeypatch):
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    # Pre-existing QSettings value should be replaced (not merged).
    isolated_settings._s.setValue("server/token", "stale")
    isolated_settings.access_token = "fresh"
    assert fake.store == {"token": "fresh"}
    assert isolated_settings._s.value("server/token", "", type=str) == "fresh"


def test_keyring_read_backfills_qsettings(isolated_settings, monkeypatch):
    # An existing install whose QSettings copy was wiped by the prior
    # migrate-and-remove code should backfill on first read so the
    # next boot has the resilience floor in place.
    fake = _FakeKeyring()
    fake.store["token"] = "in-keyring-only"
    _patch_keyring(monkeypatch, fake)
    # Clear any residual QSettings state from prior tests in this
    # session — QStandardPaths test mode is process-wide, not per-test.
    isolated_settings._s.remove("server/token")
    assert isolated_settings._s.value("server/token", "", type=str) == ""
    assert isolated_settings.access_token == "in-keyring-only"
    # After the read, QSettings now mirrors the keyring.
    assert isolated_settings._s.value("server/token", "", type=str) == "in-keyring-only"


def test_legacy_qsettings_only_falls_back_on_read(
    isolated_settings, monkeypatch,
):
    # Token only in QSettings (e.g. keyring is unavailable this run).
    # Read should return the value from the fallback path.
    fake = _FakeKeyring()
    fake.fail_get = True  # keyring backend unavailable
    _patch_keyring(monkeypatch, fake)
    isolated_settings._s.setValue("server/token", "legacy-token")
    assert isolated_settings.access_token == "legacy-token"
    # QSettings copy stays — it's the resilience floor, not a migration
    # source to be cleaned up.
    assert isolated_settings._s.value("server/token", "", type=str) == "legacy-token"


def test_token_writer_keeps_qsettings_when_keyring_broken(
    isolated_settings, monkeypatch,
):
    # Keyring write fails (no backend / denied prompt). The QSettings
    # copy still gets written so the app stays usable.
    fake = _FakeKeyring()
    fake.fail_set = True
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "fallback"
    assert fake.store == {}
    assert isolated_settings._s.value("server/token", "", type=str) == "fallback"
    # Reading still works — falls back to QSettings.
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
    # access_token = ""` — clears both surfaces. Guards against a
    # regression where logout leaks a token in plaintext after a
    # wallet write succeeds but a clear fails (or vice versa).
    fake = _FakeKeyring()
    _patch_keyring(monkeypatch, fake)
    isolated_settings.access_token = "active"
    assert isolated_settings.access_token == "active"
    isolated_settings.access_token = ""
    assert isolated_settings.access_token == ""
    assert fake.store == {}
    assert isolated_settings._s.value("server/token", "", type=str) == ""
