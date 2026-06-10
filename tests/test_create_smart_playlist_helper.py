"""Tests for ``ui_helpers.open_create_smart_playlist`` — the right-click
*Create smart playlist from this artist / album / genre* entry point.

The helper builds a preset rules dict from the matching
``smart_playlists.presets`` factory, opens the editor pre-populated, and
on save appends the new entry to ``settings.smart_playlists``. The
editor dialog is mocked here so the flow is testable headless.

Editor open is non-blocking (callback-based), so each test captures the
``on_save`` callback and invokes it directly to simulate the user
clicking Save; tests that need cancel-semantics simply don't invoke it.
"""

from __future__ import annotations

import pytest

from jellytoast import smart_playlist_editor as _editor
from jellytoast.ui_helpers import open_create_smart_playlist


@pytest.fixture
def sp_settings(isolated_settings):
    key = "library/smart_playlists"
    isolated_settings._s.remove(key)
    yield isolated_settings
    isolated_settings._s.remove(key)


def _stub_editor(monkeypatch, captured):
    """Patch ``open_smart_playlist_editor`` to capture its kwargs
    (including the ``on_save`` callback) instead of showing a dialog.
    Tests then call ``captured['on_save'](fake_entry)`` to simulate
    save, or skip it entirely to simulate cancel."""

    def _fake(
        parent=None,
        entry=None,
        preset_rules=None,
        suggested_name=None,
        hint=None,
        on_save=None,
        on_save_and_play=None,
    ):
        captured["preset_rules"] = preset_rules
        captured["suggested_name"] = suggested_name
        captured["hint"] = hint
        captured["on_save"] = on_save
        captured["on_save_and_play"] = on_save_and_play
        return None

    monkeypatch.setattr(_editor, "open_smart_playlist_editor", _fake)


class TestOpenCreateSmartPlaylist:
    def test_artist_flow_persists_entry(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "artist", "Bjork")

        # Naming idiom: "Deep Cuts: Artist" — see ui_helpers.
        assert captured["suggested_name"] == "Deep Cuts: Bjork"
        rule = captured["preset_rules"]["rules"][0]
        assert rule["field"] == "artist"
        assert rule["value"] == "Bjork"
        # User clicks Save — invoke the captured callback.
        saved = {
            "name": "Deep Cuts: Bjork",
            "rules": {"match": "all", "rules": []},
            "created_at": "2026-05-20T12:00:00",
        }
        captured["on_save"](saved)

        persisted = sp_settings.smart_playlists
        assert len(persisted) == 1
        assert persisted[0]["name"] == "Deep Cuts: Bjork"
        assert persisted[0]["created_at"] == "2026-05-20T12:00:00"

    def test_album_suggested_name(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "album", "Homogenic")

        assert captured["suggested_name"] == "More like Homogenic"
        # Name-only path yields the album not_equals exclusion rule.
        assert captured["preset_rules"]["rules"][0] == {
            "field": "album",
            "op": "not_equals",
            "value": "Homogenic",
        }

    def test_album_with_item_dict_seeds_genre_and_year(
        self, monkeypatch, sp_settings
    ):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        item = {"Name": "Homogenic", "Genres": ["Electronic"], "ProductionYear": 1997}
        open_create_smart_playlist(None, "album", "Homogenic", item=item)

        rules = captured["preset_rules"]["rules"]
        assert {"field": "genre", "op": "equals", "value": "Electronic"} in rules
        assert {"field": "year", "op": "between", "value": [1994, 2000]} in rules

    def test_genre_suggested_name(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "genre", "Trip-Hop")

        assert captured["suggested_name"] == "Trip-Hop Discoveries"
        assert captured["preset_rules"]["rules"][0]["field"] == "genre"

    def test_album_with_missing_genres_emits_hint(self, monkeypatch, sp_settings):
        """When the album has no genres tagged, the editor opens with
        a hint explaining the recipe degraded to year-only."""
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        item = {"Name": "Pocket Symphony", "Genres": [], "ProductionYear": 2007}
        open_create_smart_playlist(None, "album", "Pocket Symphony", item=item)

        assert captured["hint"] is not None
        assert "Pocket Symphony" in captured["hint"]
        assert "genre" in captured["hint"].lower()

    def test_album_with_genres_no_hint(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        item = {"Name": "Homogenic", "Genres": ["Electronic"], "ProductionYear": 1997}
        open_create_smart_playlist(None, "album", "Homogenic", item=item)

        assert captured["hint"] is None

    def test_track_with_item_dict(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        item = {"Name": "Seven Stars", "Genres": ["Electronic"], "ProductionYear": 2007}
        open_create_smart_playlist(None, "track", "Seven Stars", item=item)

        assert captured["suggested_name"] == "More like Seven Stars"
        rules = captured["preset_rules"]["rules"]
        assert {"field": "genre", "op": "equals", "value": "Electronic"} in rules
        assert {"field": "year", "op": "between", "value": [2004, 2010]} in rules

    def test_cancel_persists_nothing(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "artist", "Bjork")
        # User cancels — on_save is NOT invoked.

        assert sp_settings.smart_playlists == []

    def test_blank_name_is_a_noop(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "artist", "")

        assert captured == {}  # editor never opened

    def test_unknown_kind_is_a_noop(self, monkeypatch, sp_settings):
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "playlist", "Faves")

        assert captured == {}

    def test_save_and_play_callback_plays_after_persist(
        self, monkeypatch, sp_settings
    ):
        """Save & Play wires a callback that persists the entry AND
        calls ``smart_playlists.play_entry`` with the dismiss cb.
        Verify both happen + dismiss is forwarded to play_entry."""
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        played: list = []

        def _fake_play_entry(entry, parent=None, on_complete=None):
            played.append((entry, on_complete))

        # Patch play_entry at the import-source so the lazy import in
        # _on_save_and_play picks up the stub.
        from jellytoast.smart_playlists import play as _play_mod
        monkeypatch.setattr(_play_mod, "play_entry", _fake_play_entry)

        open_create_smart_playlist(None, "artist", "Bjork")

        saved = {
            "name": "Deep Cuts: Bjork",
            "rules": {"match": "all", "rules": []},
            "created_at": "2026-05-20T12:00:00",
        }
        # Caller wires on_save_and_play — simulate the editor's
        # invocation with (entry, dismiss).
        assert captured["on_save_and_play"] is not None
        dismiss_calls: list = []
        captured["on_save_and_play"](saved, lambda: dismiss_calls.append(1))

        # Persisted...
        persisted = sp_settings.smart_playlists
        assert len(persisted) == 1
        assert persisted[0]["name"] == "Deep Cuts: Bjork"
        # ...and play_entry was called with the same entry + the
        # dismiss cb threaded through as on_complete (NOT invoked
        # yet — the editor's loading state stays until async work
        # signals back via on_complete).
        assert len(played) == 1
        played_entry, played_on_complete = played[0]
        assert played_entry == saved
        assert played_on_complete is not None
        assert dismiss_calls == []  # not fired yet
        # Simulating play_entry's async completion firing on_complete
        # should fire dismiss.
        played_on_complete()
        assert dismiss_calls == [1]

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
        captured: dict = {}
        _stub_editor(monkeypatch, captured)

        open_create_smart_playlist(None, "artist", "Bjork")
        captured["on_save"]({
            "name": "More by Bjork",
            "rules": {"match": "all", "rules": []},
            "created_at": "2026-05-20T12:00:00",
        })

        names = [e["name"] for e in sp_settings.smart_playlists]
        assert names == ["Top played", "More by Bjork"]
