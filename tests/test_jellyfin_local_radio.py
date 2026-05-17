"""Tests for JellyfinProvider's local internet-radio CRUD.

Jellyfin has no native internet-radio endpoint, so the provider stores
stations in QSettings under ``radio/stations`` as a JSON list. The four
``*_internet_radio_station`` methods all flow through
``Settings.radio_stations``; these tests exercise that store directly
plus the parity-with-Subsonic contract on the returned dict shape.
"""

from __future__ import annotations

import json

import pytest

import modules.settings as smod
from modules.providers.jellyfin import JellyfinProvider
from modules.providers.subsonic import SubsonicProvider


@pytest.fixture
def fresh_settings(isolated_settings, monkeypatch):
    """Wire the module-level ``get_settings()`` singleton at the swap
    point — ``modules.settings._settings`` — to a fresh per-test
    ``Settings`` so JellyfinProvider's lazy ``get_settings()`` lookups
    land on the isolated instance. ``isolated_settings`` (conftest)
    already has its QSettings in test-mode, but we still need to nuke
    the ``radio/stations`` key for cleanliness between tests since
    QSettings test-mode shares one file process-wide.
    """
    isolated_settings._s.remove("radio/stations")
    monkeypatch.setattr(smod, "_settings", isolated_settings)
    yield isolated_settings
    isolated_settings._s.remove("radio/stations")


@pytest.fixture
def provider(fresh_settings):
    return JellyfinProvider()


# ── get_internet_radio_stations ─────────────────────────────────────────


class TestGetInternetRadioStations:
    def test_empty_initial_state(self, provider):
        assert provider.get_internet_radio_stations() == []

    def test_returns_deep_copy(self, provider):
        provider.create_internet_radio_station("KEXP", "https://kexp/")
        first = provider.get_internet_radio_stations()
        first.clear()
        first.append({"id": "x", "name": "mutated"})
        second = provider.get_internet_radio_stations()
        assert len(second) == 1
        assert second[0]["name"] == "KEXP"

    def test_inner_dicts_are_copies(self, provider):
        provider.create_internet_radio_station("KEXP", "https://kexp/")
        first = provider.get_internet_radio_stations()
        first[0]["name"] = "mutated"
        second = provider.get_internet_radio_stations()
        assert second[0]["name"] == "KEXP"


# ── create_internet_radio_station ───────────────────────────────────────


class TestCreateInternetRadioStation:
    def test_returns_dict_with_generated_id(self, provider):
        result = provider.create_internet_radio_station(
            "KEXP", "https://kexp/stream", "https://kexp.org"
        )
        assert result["name"] == "KEXP"
        assert result["streamUrl"] == "https://kexp/stream"
        assert result["homePageUrl"] == "https://kexp.org"
        assert result["id"].startswith("local-")
        assert len(result["id"]) > len("local-")

    def test_station_persisted_in_list(self, provider):
        provider.create_internet_radio_station("KEXP", "https://kexp/")
        stations = provider.get_internet_radio_stations()
        assert len(stations) == 1
        assert stations[0]["name"] == "KEXP"

    def test_two_creates_get_unique_ids(self, provider):
        a = provider.create_internet_radio_station("KEXP", "https://k/")
        b = provider.create_internet_radio_station("KCRW", "https://c/")
        assert a["id"] != b["id"]

    def test_creation_order_preserved(self, provider):
        provider.create_internet_radio_station("KEXP", "https://k/")
        provider.create_internet_radio_station("KCRW", "https://c/")
        provider.create_internet_radio_station("BBC6", "https://b/")
        names = [s["name"] for s in provider.get_internet_radio_stations()]
        assert names == ["KEXP", "KCRW", "BBC6"]

    def test_home_page_url_optional(self, provider):
        result = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        assert result["homePageUrl"] == ""


# ── update_internet_radio_station ───────────────────────────────────────


class TestUpdateInternetRadioStation:
    def test_update_existing_changes_fields(self, provider):
        created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/", "https://kexp.org"
        )
        updated = provider.update_internet_radio_station(
            created["id"], "KEXP 90.3", "https://kexp/new",
            "https://kexp.org/new",
        )
        assert updated["id"] == created["id"]
        assert updated["name"] == "KEXP 90.3"
        assert updated["streamUrl"] == "https://kexp/new"
        assert updated["homePageUrl"] == "https://kexp.org/new"

    def test_update_persists(self, provider):
        created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        provider.update_internet_radio_station(
            created["id"], "KEXP 90.3", "https://kexp/new"
        )
        stations = provider.get_internet_radio_stations()
        assert stations[0]["name"] == "KEXP 90.3"

    def test_update_id_preserved(self, provider):
        created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        provider.update_internet_radio_station(
            created["id"], "new name", "https://new/"
        )
        stations = provider.get_internet_radio_stations()
        assert stations[0]["id"] == created["id"]

    def test_update_missing_id_raises(self, provider):
        with pytest.raises(ValueError):
            provider.update_internet_radio_station(
                "local-nope", "X", "https://x/"
            )

    def test_update_only_target(self, provider):
        a = provider.create_internet_radio_station("KEXP", "https://k/")
        b = provider.create_internet_radio_station("KCRW", "https://c/")
        provider.update_internet_radio_station(
            b["id"], "KCRW HD", "https://c/hd"
        )
        stations = provider.get_internet_radio_stations()
        names_by_id = {s["id"]: s["name"] for s in stations}
        assert names_by_id[a["id"]] == "KEXP"
        assert names_by_id[b["id"]] == "KCRW HD"


# ── delete_internet_radio_station ───────────────────────────────────────


class TestDeleteInternetRadioStation:
    def test_delete_existing(self, provider):
        created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        provider.delete_internet_radio_station(created["id"])
        assert provider.get_internet_radio_stations() == []

    def test_delete_missing_is_idempotent(self, provider):
        # No exception even though the id doesn't exist.
        provider.delete_internet_radio_station("local-nope")

    def test_delete_missing_with_existing_stations_is_idempotent(
            self, provider):
        provider.create_internet_radio_station("KEXP", "https://k/")
        provider.delete_internet_radio_station("local-nope")
        assert len(provider.get_internet_radio_stations()) == 1

    def test_delete_returns_none(self, provider):
        created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        result = provider.delete_internet_radio_station(created["id"])
        assert result is None

    def test_delete_one_keeps_others(self, provider):
        a = provider.create_internet_radio_station("KEXP", "https://k/")
        b = provider.create_internet_radio_station("KCRW", "https://c/")
        provider.delete_internet_radio_station(a["id"])
        stations = provider.get_internet_radio_stations()
        assert len(stations) == 1
        assert stations[0]["id"] == b["id"]


# ── Settings round-trip ─────────────────────────────────────────────────


class TestSettingsRoundTrip:
    def test_round_trip_via_new_settings_instance(self, fresh_settings,
                                                  monkeypatch):
        # Write through one Settings instance ...
        fresh_settings.radio_stations = [{
            "id": "local-abc",
            "name": "KEXP",
            "streamUrl": "https://kexp/",
            "homePageUrl": "https://kexp.org",
        }]
        fresh_settings.flush()

        # ... and read back through a fresh instance (simulates a
        # process restart over the same QSettings store).
        s2 = smod.Settings()
        loaded = s2.radio_stations
        assert loaded == [{
            "id": "local-abc",
            "name": "KEXP",
            "streamUrl": "https://kexp/",
            "homePageUrl": "https://kexp.org",
        }]

    def test_round_trip_through_provider(self, provider, fresh_settings):
        provider.create_internet_radio_station(
            "KEXP", "https://kexp/", "https://kexp.org"
        )
        fresh_settings.flush()
        # Fresh provider + fresh settings should see the same station.
        s2 = smod.Settings()
        # Briefly re-point the singleton so the new provider lookup
        # finds s2.
        import modules.settings as _sm
        prev = _sm._settings
        _sm._settings = s2
        try:
            provider2 = JellyfinProvider()
            loaded = provider2.get_internet_radio_stations()
            assert len(loaded) == 1
            assert loaded[0]["name"] == "KEXP"
        finally:
            _sm._settings = prev


# ── Cross-provider parity ───────────────────────────────────────────────


class TestCrossProviderParity:
    """The whole point of this work — Jellyfin and Subsonic return the
    same dict-key shape so the UI never branches on provider kind."""

    def test_dict_key_shape_matches_subsonic(self, provider, monkeypatch):
        # Subsonic returns whatever the server sends; we hard-stub a
        # representative response and assert that the keys Jellyfin
        # emits are a superset/equal.
        jf_created = provider.create_internet_radio_station(
            "KEXP", "https://kexp/stream", "https://kexp.org"
        )
        jf_keys = set(jf_created.keys())
        # Canonical keys from Subsonic's getInternetRadioStations (see
        # modules/providers/subsonic.py ~1115-1127).
        subsonic_keys = {"id", "name", "streamUrl", "homePageUrl"}
        assert jf_keys == subsonic_keys

    def test_get_returns_matching_keys(self, provider):
        provider.create_internet_radio_station("KEXP", "https://kexp/")
        stations = provider.get_internet_radio_stations()
        assert set(stations[0].keys()) == {
            "id", "name", "streamUrl", "homePageUrl"
        }

    def test_subsonic_emits_same_keys(self, monkeypatch):
        # Mock SubsonicAPI._request so we can drive a canned response
        # without a real server, then assert the parsed dict shape.
        sp = SubsonicProvider()

        def fake_request(action, params=None):
            assert action == "getInternetRadioStations"
            return {
                "internetRadioStations": {
                    "internetRadioStation": [{
                        "id": "1",
                        "name": "KEXP",
                        "streamUrl": "https://kexp/",
                        "homePageUrl": "https://kexp.org",
                    }],
                },
            }

        monkeypatch.setattr(sp, "_request", fake_request)
        result = sp.get_internet_radio_stations()
        assert len(result) == 1
        # All four keys are present; Subsonic-emitted servers may
        # include extras (e.g. coverArt) but the contract is that
        # these four are always there.
        for key in ("id", "name", "streamUrl", "homePageUrl"):
            assert key in result[0]


# ── Resilience ──────────────────────────────────────────────────────────


class TestResilience:
    def test_invalid_json_returns_empty_list(self, fresh_settings, capsys):
        fresh_settings._s.setValue("radio/stations", "not-json-at-all")
        result = fresh_settings.radio_stations
        assert result == []
        captured = capsys.readouterr()
        assert "radio/stations" in captured.out

    def test_non_list_json_returns_empty_list(self, fresh_settings, capsys):
        fresh_settings._s.setValue(
            "radio/stations", json.dumps({"not": "a list"})
        )
        result = fresh_settings.radio_stations
        assert result == []
        captured = capsys.readouterr()
        assert "radio/stations" in captured.out

    def test_malformed_entry_dropped(self, fresh_settings, capsys):
        fresh_settings._s.setValue(
            "radio/stations",
            json.dumps([
                {"id": "good", "name": "KEXP", "streamUrl": "https://k/"},
                {"name": "incomplete"},  # missing id + streamUrl
                "not a dict",
            ]),
        )
        result = fresh_settings.radio_stations
        assert len(result) == 1
        assert result[0]["id"] == "good"

    def test_empty_string_returns_empty_list(self, fresh_settings):
        fresh_settings._s.setValue("radio/stations", "")
        assert fresh_settings.radio_stations == []

    def test_provider_survives_invalid_store(self, provider,
                                              fresh_settings):
        fresh_settings._s.setValue("radio/stations", "garbage")
        # get returns empty, no exception
        assert provider.get_internet_radio_stations() == []
        # create still works — overwrites the garbage with a fresh list
        result = provider.create_internet_radio_station(
            "KEXP", "https://kexp/"
        )
        assert result["name"] == "KEXP"
        assert len(provider.get_internet_radio_stations()) == 1
