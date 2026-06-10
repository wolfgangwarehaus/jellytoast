"""Tests for AirPlay 2 HAP pairing-credential storage (jellytoast.airplay2).

Credentials are encrypted at rest with the same AES-GCM machine-key
scheme the access token + ListenBrainz token use — never plaintext in
QSettings. Reads forward-migrate any legacy plaintext value (stored
before encryption shipped) so existing pairings survive the upgrade.

`isolated_settings` (conftest.py) pins the Settings singleton, so the
module-level `get_settings()` the airplay2 helpers call resolves to the
same in-memory QSettings store this test inspects.
"""

from jellytoast import airplay2
from jellytoast.settings import _ENC_PREFIX, _decrypt_token


def _raw(settings, identifier):
    return settings._s.value(airplay2._credentials_key(identifier), "", type=str)


def test_round_trip_returns_original(isolated_settings):
    airplay2.store_credentials("dev-1", "MII-pairing-blob")
    assert airplay2.get_stored_credentials("dev-1") == "MII-pairing-blob"


def test_stored_value_is_encrypted_not_plaintext(isolated_settings):
    airplay2.store_credentials("dev-1", "MII-pairing-blob")
    raw = _raw(isolated_settings, "dev-1")
    # On-disk value is an AES-GCM blob with the version prefix — the
    # plaintext credential must never appear in QSettings.
    assert raw.startswith(_ENC_PREFIX)
    assert "MII-pairing-blob" not in raw
    assert _decrypt_token(raw) == "MII-pairing-blob"


def test_unpaired_device_returns_empty(isolated_settings):
    assert airplay2.get_stored_credentials("never-paired") == ""


def test_empty_store_writes_empty(isolated_settings):
    airplay2.store_credentials("dev-1", "")
    assert _raw(isolated_settings, "dev-1") == ""
    assert airplay2.get_stored_credentials("dev-1") == ""


def test_legacy_plaintext_is_read_and_upgraded(isolated_settings):
    # Pre-encryption install: a plaintext blob already on disk. The read
    # must return it unchanged AND re-write it encrypted so subsequent
    # reads no longer leak plaintext — existing pairings keep working.
    key = airplay2._credentials_key("dev-legacy")
    isolated_settings._s.setValue(key, "legacy-plaintext-creds")
    assert airplay2.get_stored_credentials("dev-legacy") == "legacy-plaintext-creds"
    raw = _raw(isolated_settings, "dev-legacy")
    assert raw.startswith(_ENC_PREFIX)
    assert "legacy-plaintext-creds" not in raw
    assert _decrypt_token(raw) == "legacy-plaintext-creds"


def test_forget_removes_credentials(isolated_settings):
    airplay2.store_credentials("dev-1", "blob")
    airplay2.forget_credentials("dev-1")
    assert airplay2.get_stored_credentials("dev-1") == ""


# ── LAN-interface binding for the mDNS scan (Tailscale fix) ───────────────────


class TestAirplayLanAiozc:
    """pyatv's mDNS scan is bound to an AsyncZeroconf on the LAN interfaces
    (Tailscale/CGNAT excluded) — on a Tailscale host the default scan leaves
    via the tunnel and finds no AirPlay devices. The helper degrades to None
    (pyatv default-binds) when interfaces can't be enumerated."""

    def test_aiozc_none_without_interfaces(self, monkeypatch):
        import jellytoast.airplay2 as ap2
        import jellytoast.cast_manager as cm

        monkeypatch.setattr(cm, "_discovery_interfaces", lambda: None)
        assert ap2._lan_aiozc() is None
        monkeypatch.setattr(cm, "_discovery_interfaces", lambda: [])
        assert ap2._lan_aiozc() is None

    def test_aiozc_swallows_errors(self, monkeypatch):
        import jellytoast.airplay2 as ap2
        import jellytoast.cast_manager as cm

        def _boom():
            raise RuntimeError("ifaddr exploded")

        monkeypatch.setattr(cm, "_discovery_interfaces", _boom)
        assert ap2._lan_aiozc() is None
