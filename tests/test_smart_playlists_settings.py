"""Tests for ``settings.smart_playlists`` — the persistence boundary
for user-defined smart playlists. The setter validates each entry
against ``modules.providers.smart_rule_schema.validate_rules`` and
drops anything malformed so a corrupted settings file degrades cleanly
instead of crashing the app on boot.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sp_settings(isolated_settings):
    key = "library/smart_playlists"
    isolated_settings._s.remove(key)
    yield isolated_settings
    isolated_settings._s.remove(key)


_VALID_RULES = {
    "match": "all",
    "rules": [{"field": "play_count", "op": "greater_than", "value": 0}],
    "limit": 100,
    "sort": "play_count",
    "sort_desc": True,
}


_VALID_ENTRY = {
    "name": "Top played",
    "rules": _VALID_RULES,
    "created_at": "2026-05-19T15:00:00",
}


class TestSmartPlaylistsSetting:
    def test_default_is_empty_list(self, sp_settings):
        assert sp_settings.smart_playlists == []

    def test_round_trip_valid_entry(self, sp_settings):
        sp_settings.smart_playlists = [_VALID_ENTRY]
        out = sp_settings.smart_playlists
        assert len(out) == 1
        assert out[0]["name"] == "Top played"
        assert out[0]["rules"]["match"] == "all"
        assert out[0]["rules"]["rules"][0]["field"] == "play_count"
        assert out[0]["created_at"] == "2026-05-19T15:00:00"

    def test_empty_name_dropped(self, sp_settings):
        sp_settings.smart_playlists = [
            _VALID_ENTRY,
            {"name": "", "rules": _VALID_RULES, "created_at": ""},
            {"name": "   ", "rules": _VALID_RULES, "created_at": ""},
        ]
        out = sp_settings.smart_playlists
        assert len(out) == 1
        assert out[0]["name"] == "Top played"

    def test_invalid_rules_dropped(self, sp_settings):
        # match must be one of {"all", "any"} — "wrong" is rejected by
        # ``validate_rules``, so the whole entry should be dropped.
        sp_settings.smart_playlists = [
            _VALID_ENTRY,
            {
                "name": "Bogus",
                "rules": {"match": "wrong", "rules": []},
                "created_at": "",
            },
        ]
        out = sp_settings.smart_playlists
        assert len(out) == 1
        assert out[0]["name"] == "Top played"

    def test_non_dict_entries_ignored(self, sp_settings):
        sp_settings.smart_playlists = [
            _VALID_ENTRY,
            "not a dict",
            None,
            ["also wrong"],
        ]
        assert len(sp_settings.smart_playlists) == 1

    def test_name_whitespace_stripped(self, sp_settings):
        sp_settings.smart_playlists = [
            {**_VALID_ENTRY, "name": "  Trimmed  "},
        ]
        assert sp_settings.smart_playlists[0]["name"] == "Trimmed"

    def test_malformed_json_returns_empty(self, sp_settings):
        # Stuff a corrupted JSON string directly under the QSettings key
        # so the getter has to recover gracefully.
        sp_settings._s.setValue("library/smart_playlists", "{not json")
        assert sp_settings.smart_playlists == []

    def test_setter_with_non_list_writes_empty(self, sp_settings):
        sp_settings.smart_playlists = "not a list"  # type: ignore[assignment]
        assert sp_settings.smart_playlists == []
