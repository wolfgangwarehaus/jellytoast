"""Tests for ``ui_helpers.open_create_smart_playlist`` — the right-click
*Create smart playlist from this artist / album / genre* entry point.

The helper builds a preset rules dict from the matching
``smart_playlists.presets`` factory, opens the editor pre-populated, and
on save appends the new entry to ``settings.smart_playlists``. The
editor dialog is mocked here so the flow is testable headless.
"""

from __future__ import annotations

import pytest

from modules import smart_playlist_editor as _editor
from modules.ui_helpers import open_create_smart_playlist


@pytest.fixture
def sp_settings(isolated_settings):
    key = "library/smart_playlists"
    isolated_settings._s.remove(key)
    yield isolated_settings
    isolated_settings._s.remove(key)


def _stub_editor(monkeypatch, return_value, captured):
    """Patch ``open_smart_playlist_editor`` to capture its kwargs and
    return ``return_value`` instead of showing a dialog."""

    def _fake(parent=None, entry=None, preset_rules=None, suggested_name=None):
        captured["preset_rules"] = preset_rules
        captured["suggested_name"] = suggested_name
        return return_value

    monkeypatch.setattr(_editor, "open_smart_playlist_editor", _fake)


class TestOpenCreateSmartPlaylist:
    def test_artist_flow_persists_entry(self, monkeypatch, sp_settings):
        captured: dict = {}
        saved = {
            "name": "More by Bjork",
            "rules": {"match": "all", "rules": []},
            "created_at": "2026-05-20T12:00:00",
        }
        _stub_editor(monkeypatch, saved, captured)

        result = open_create_smart_playlist(None, "artist", "Bjork")

        assert result is True
        assert captured["suggested_name"] == "More by Bjork"
        # The artist factory keys an ``artist equals`` rule.
        rule = captured["preset_rules"]["rules"][0]
        assert rule["field"] == "artist"
        assert rule["value"] == "Bjork"
        # The settings setter may stamp schema_version on persist, so
        # compare identifying fields rather than the whole dict.
        persisted = sp_settings.smart_playlists
        assert len(persisted) == 1
        assert persisted[0]["name"] == "More by Bjork"
        assert persisted[0]["created_at"] == "2026-05-20T12:00:00"

    def test_album_suggested_name(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, None, captured)

        open_create_smart_playlist(None, "album", "Homogenic")

        assert captured["suggested_name"] == "Homogenic (album)"
        assert captured["preset_rules"]["rules"][0]["field"] == "album"

    def test_genre_suggested_name(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, None, captured)

        open_create_smart_playlist(None, "genre", "Trip-Hop")

        assert captured["suggested_name"] == "Trip-Hop mix"
        assert captured["preset_rules"]["rules"][0]["field"] == "genre"

    def test_cancel_returns_false_and_persists_nothing(
        self, monkeypatch, sp_settings
    ):
        _stub_editor(monkeypatch, None, {})

        result = open_create_smart_playlist(None, "artist", "Bjork")

        assert result is False
        assert sp_settings.smart_playlists == []

    def test_blank_name_is_a_noop(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, {"name": "x"}, captured)

        result = open_create_smart_playlist(None, "artist", "")

        assert result is False
        assert captured == {}  # editor never opened

    def test_unknown_kind_is_a_noop(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, {"name": "x"}, captured)

        result = open_create_smart_playlist(None, "playlist", "Faves")

        assert result is False
        assert captured == {}

    def test_save_appends_without_clobbering_existing(
        self, monkeypatch, sp_settings
    ):
        existing = {
            "name": "Top played",
            "rules": {
                "match": "all",
                "rules": [
                    {"field": "play_count", "op": "greater_than", "value": 0}
                ],
                "limit": 100,
                "sort": "play_count",
                "sort_desc": True,
            },
            "created_at": "2026-05-19T15:00:00",
        }
        sp_settings.smart_playlists = [existing]
        new = {
            "name": "More by Bjork",
            "rules": {"match": "all", "rules": []},
            "created_at": "2026-05-20T12:00:00",
        }
        _stub_editor(monkeypatch, new, {})

        open_create_smart_playlist(None, "artist", "Bjork")

        names = [e["name"] for e in sp_settings.smart_playlists]
        assert names == ["Top played", "More by Bjork"]
