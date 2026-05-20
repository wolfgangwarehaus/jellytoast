"""Tests for the smart-playlist editor's preset-rules wiring.

The right-click "Create smart playlist from this artist / album /
genre" flow constructs the editor with a pre-populated rules dict
(from a ``modules.smart_playlists.presets`` ``from_*`` factory) plus a
suggested name. These tests build the dialog directly — no event loop
runs, so the debounced preview timer never fires and no provider is
touched.
"""

from __future__ import annotations

from modules.smart_playlist_editor import SmartPlaylistEditorDialog
from modules.smart_playlists import from_artist, from_genre


class TestPresetRulesSeeding:
    def test_preset_rules_populate_chips(self, qapp):
        rules = from_artist("Bjork")
        dlg = SmartPlaylistEditorDialog(preset_rules=rules)
        try:
            out = dlg.rules_dict()
            assert out["rules"] == [
                {"field": "artist", "op": "equals", "value": "Bjork"}
            ]
            assert out["sort"] == "play_count"
            assert out["sort_desc"] is True
            assert out["limit"] == 50
        finally:
            dlg.deleteLater()

    def test_suggested_name_prefills_name_field(self, qapp):
        dlg = SmartPlaylistEditorDialog(
            preset_rules=from_artist("Bjork"),
            suggested_name="More by Bjork",
        )
        try:
            assert dlg.values()["name"] == "More by Bjork"
        finally:
            dlg.deleteLater()

    def test_genre_preset_round_trips_through_editor(self, qapp):
        dlg = SmartPlaylistEditorDialog(
            preset_rules=from_genre("Jazz"),
            suggested_name="Jazz picks",
        )
        try:
            out = dlg.rules_dict()
            assert out["rules"] == [
                {"field": "genre", "op": "equals", "value": "Jazz"}
            ]
            assert out["limit"] == 100
        finally:
            dlg.deleteLater()

    def test_editing_entry_wins_over_preset_rules(self, qapp):
        # When both an existing entry and preset_rules are passed,
        # editing the entry takes priority.
        entry = {
            "name": "Existing",
            "rules": {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 1999}],
                "limit": None,
                "sort": None,
                "sort_desc": False,
            },
            "created_at": "2026-01-01T00:00:00",
        }
        dlg = SmartPlaylistEditorDialog(
            entry=entry,
            preset_rules=from_artist("Bjork"),
            suggested_name="More by Bjork",
        )
        try:
            out = dlg.rules_dict()
            assert out["rules"] == [
                {"field": "year", "op": "equals", "value": 1999}
            ]
            # Entry name wins over suggested_name.
            assert dlg.values()["name"] == "Existing"
        finally:
            dlg.deleteLater()

    def test_no_preset_rules_yields_empty_rule_set(self, qapp):
        dlg = SmartPlaylistEditorDialog()
        try:
            assert dlg.rules_dict()["rules"] == []
        finally:
            dlg.deleteLater()


class TestIsFavoriteChipRoundTrip:
    def test_is_favorite_rule_round_trips(self, qapp):
        rules = {
            "match": "all",
            "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            "limit": None,
            "sort": None,
            "sort_desc": False,
        }
        dlg = SmartPlaylistEditorDialog(preset_rules=rules)
        try:
            out = dlg.rules_dict()
            assert out["rules"] == [
                {"field": "is_favorite", "op": "equals", "value": True}
            ]
            # The value must come back as a real bool, not a string.
            assert out["rules"][0]["value"] is True
        finally:
            dlg.deleteLater()

    def test_random_sort_round_trips(self, qapp):
        rules = {
            "match": "all",
            "rules": [],
            "limit": None,
            "sort": "random",
            "sort_desc": False,
        }
        dlg = SmartPlaylistEditorDialog(preset_rules=rules)
        try:
            assert dlg.rules_dict()["sort"] == "random"
        finally:
            dlg.deleteLater()
