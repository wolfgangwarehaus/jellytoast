"""Tests for the smart-playlist starter presets.

Each starter must validate against the v1 schema and round-trip
through the Python refinement layer without producing surprises.
"""

from __future__ import annotations

from modules.providers.smart_rule_eval import refine_items
from modules.providers.smart_rule_schema import validate_rules
from modules.smart_playlists import (
    PRESETS,
    YEAR_PRESET_NAME,
    get_preset,
    make_year_preset,
)


def _item(id_, *, year=2020, play_count=0, album_artist="AA"):
    return {
        "Id": id_,
        "Name": f"Track {id_}",
        "Type": "Audio",
        "ProductionYear": year,
        "Genres": [],
        "Artists": [album_artist],
        "AlbumArtist": album_artist,
        "Album": "Al",
        "UserData": {"PlayCount": play_count},
    }


class TestPresetsValidate:
    def test_every_concrete_preset_validates(self):
        for name, _description, _friendly, rules in PRESETS:
            if name == YEAR_PRESET_NAME:
                continue
            errors = validate_rules(rules)
            assert errors == [], f"{name}: {errors}"

    def test_year_factory_validates(self):
        rules = make_year_preset(2020)
        assert validate_rules(rules) == []


class TestGetPreset:
    def test_returns_dict_for_known_name(self):
        rules = get_preset("Top played")
        assert isinstance(rules, dict)
        assert rules["sort"] == "play_count"
        assert rules["sort_desc"] is True
        assert rules["limit"] == 100

    def test_recently_added_uses_year_proxy(self):
        rules = get_preset("Recently added")
        assert isinstance(rules, dict)
        assert any(r["field"] == "year" for r in rules["rules"])

    def test_forgotten_favorites_filters_play_count(self):
        rules = get_preset("Forgotten favorites")
        assert isinstance(rules, dict)
        play_count_rules = [r for r in rules["rules"] if r["field"] == "play_count"]
        assert play_count_rules
        assert play_count_rules[0]["op"] == "greater_than"
        assert play_count_rules[0]["value"] == 5
        assert rules["limit"] == 50

    def test_returns_none_for_unknown_name(self):
        assert get_preset("Nope") is None
        assert get_preset("") is None


class TestMakeYearPreset:
    def test_value_round_trips(self):
        rules = make_year_preset(2020)
        year_rule = next(r for r in rules["rules"] if r["field"] == "year")
        assert year_rule["op"] == "equals"
        assert year_rule["value"] == 2020

    def test_no_limit(self):
        assert make_year_preset(1999)["limit"] is None

    def test_sort_by_artist_ascending(self):
        rules = make_year_preset(2010)
        assert rules["sort"] == "artist"
        assert rules["sort_desc"] is False


class TestEvaluatorIntegration:
    def test_top_played_filters_and_sorts(self):
        items = [
            _item("a", play_count=0),
            _item("b", play_count=3),
            _item("c", play_count=10),
            _item("d", play_count=1),
        ]
        rules = get_preset("Top played")
        result = refine_items(items, rules)
        assert [it["Id"] for it in result] == ["c", "b", "d"]

    def test_top_played_limit_caps_results(self):
        items = [_item(str(i), play_count=i + 1) for i in range(150)]
        rules = get_preset("Top played")
        result = refine_items(items, rules)
        assert len(result) == 100
        assert result[0]["UserData"]["PlayCount"] == 150

    def test_forgotten_favorites_threshold(self):
        items = [
            _item("low", play_count=2),
            _item("mid", play_count=6),
            _item("hi", play_count=20),
        ]
        rules = get_preset("Forgotten favorites")
        result = refine_items(items, rules)
        assert [it["Id"] for it in result] == ["hi", "mid"]

    def test_year_preset_filters_by_year(self):
        items = [
            _item("a", year=2019),
            _item("b", year=2020, album_artist="Zed"),
            _item("c", year=2020, album_artist="Abba"),
            _item("d", year=2021),
        ]
        rules = make_year_preset(2020)
        result = refine_items(items, rules)
        assert [it["Id"] for it in result] == ["c", "b"]


class TestPresetsShape:
    def test_presets_tuple_shape(self):
        assert isinstance(PRESETS, list)
        assert len(PRESETS) == 4
        for entry in PRESETS:
            assert isinstance(entry, tuple)
            assert len(entry) == 4
            name, description, friendly, rules = entry
            assert isinstance(name, str) and name
            assert isinstance(description, str) and description
            assert isinstance(friendly, str) and friendly
            assert isinstance(rules, dict)

    def test_year_custom_entry_is_placeholder(self):
        names = [entry[0] for entry in PRESETS]
        assert YEAR_PRESET_NAME in names
        idx = names.index(YEAR_PRESET_NAME)
        assert PRESETS[idx][3] == {}

    def test_preset_names_are_unique(self):
        names = [entry[0] for entry in PRESETS]
        assert len(names) == len(set(names))
