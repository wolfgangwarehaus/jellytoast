"""Tests for the smart-playlist starter presets.

Each starter must validate against the v1 schema and round-trip
through the Python refinement layer without producing surprises.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from modules.providers.smart_rule_eval import refine_items
from modules.providers.smart_rule_schema import validate_rules
from modules.smart_playlists import (
    PRESETS,
    YEAR_PRESET_NAME,
    from_album,
    from_artist,
    from_genre,
    from_track,
    get_preset,
    make_year_preset,
)


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).date().isoformat()


def _item(
    id_,
    *,
    year=2020,
    play_count=0,
    album_artist="AA",
    date_added=None,
    last_played=None,
):
    item = {
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
    if date_added is not None:
        item["DateCreated"] = date_added
    if last_played is not None:
        item["UserData"]["LastPlayedDate"] = last_played
    return item


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

    def test_recently_added_uses_date_added_field(self):
        # Schema v2: the proxy `year` rule is gone — the preset filters
        # on the real date_added field with the in_the_last operator.
        rules = get_preset("Recently added")
        assert isinstance(rules, dict)
        assert not any(r["field"] == "year" for r in rules["rules"])
        date_rules = [r for r in rules["rules"] if r["field"] == "date_added"]
        assert len(date_rules) == 1
        assert date_rules[0]["op"] == "in_the_last"
        assert date_rules[0]["value"] == 60
        assert rules["sort"] == "date_added"
        assert rules["sort_desc"] is True

    def test_returns_none_for_unknown_name(self):
        assert get_preset("Nope") is None
        assert get_preset("") is None

    def test_forgotten_favorites_preset_was_removed(self):
        # Dropped 2026-05-20: permanently empty on Subsonic (no
        # per-track last-played timestamp) and needs aged listening
        # history to populate on Jellyfin — an awkward starter.
        assert get_preset("Forgotten favorites") is None


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

    def test_recently_added_filters_and_sorts(self):
        items = [
            _item("old", date_added=_iso_days_ago(200)),
            _item("fresh", date_added=_iso_days_ago(5)),
            _item("mid", date_added=_iso_days_ago(40)),
            _item("edge", date_added=_iso_days_ago(59)),
            # No date at all — date_added rule non-matches.
            _item("undated"),
        ]
        rules = get_preset("Recently added")
        result = refine_items(items, rules)
        # Within 60 days, newest first.
        assert [it["Id"] for it in result] == ["fresh", "mid", "edge"]

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
        assert len(PRESETS) == 3
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


# ─────────────────────────────────────────────────────────────────────
# "Create from this X" recipe factories
# ─────────────────────────────────────────────────────────────────────


def _item_full(
    id_,
    *,
    year=2020,
    play_count=0,
    artists=None,
    album_artist="AA",
    album="Al",
    genres=None,
):
    return {
        "Id": id_,
        "Name": f"Track {id_}",
        "Type": "Audio",
        "ProductionYear": year,
        "Genres": list(genres or []),
        "Artists": list(artists or [album_artist]),
        "AlbumArtist": album_artist,
        "Album": album,
        "UserData": {"PlayCount": play_count, "IsFavorite": False},
    }


class TestFromArtist:
    """Deep Cuts: artist=X AND play_count<3, random, cap 50."""

    def test_validates(self):
        assert validate_rules(from_artist("Bjork")) == []

    def test_rule_shape(self):
        rules = from_artist("Bjork")
        assert rules["rules"] == [
            {"field": "artist", "op": "equals", "value": "Bjork"},
            {"field": "play_count", "op": "less_than", "value": 3},
        ]
        assert rules["limit"] == 50
        assert rules["sort"] == "random"
        assert rules["sort_desc"] is False

    def test_excludes_well_played_tracks(self):
        items = [
            _item_full("under", artists=["Bjork"], play_count=2),
            _item_full("at_threshold", artists=["Bjork"], play_count=3),
            _item_full("heavy", artists=["Bjork"], play_count=99),
            _item_full("other_artist", artists=["Air"], play_count=0),
        ]
        out = refine_items(items, from_artist("Bjork"))
        ids = {it["Id"] for it in out}
        # Only the low-play Bjork track survives.
        assert ids == {"under"}

    def test_caps_at_50(self):
        items = [
            _item_full(str(i), artists=["Bjork"], play_count=0)
            for i in range(80)
        ]
        out = refine_items(items, from_artist("Bjork"))
        assert len(out) == 50

    def test_coerces_name_to_str(self):
        rules = from_artist(12345)  # type: ignore[arg-type]
        assert rules["rules"][0]["value"] == "12345"
        assert validate_rules(rules) == []


class TestFromAlbum:
    """More like Album: genre + year window + exclude album, random, cap 50."""

    _ALBUM = {
        "Name": "Homogenic",
        "Genres": ["Electronic"],
        "ProductionYear": 1997,
    }

    def test_validates(self):
        assert validate_rules(from_album(self._ALBUM)) == []

    def test_rule_shape_from_item(self):
        rules = from_album(self._ALBUM)
        assert rules["rules"] == [
            {"field": "genre", "op": "equals", "value": "Electronic"},
            {"field": "year", "op": "between", "value": [1994, 2000]},
            {"field": "album", "op": "not_equals", "value": "Homogenic"},
        ]
        assert rules["limit"] == 50
        assert rules["sort"] == "random"

    def test_falls_back_to_name_only_string_arg(self):
        # Legacy callers passing just a name still get a valid rule
        # set — just without the genre / year refinements.
        rules = from_album("Homogenic")
        assert rules["rules"] == [
            {"field": "album", "op": "not_equals", "value": "Homogenic"}
        ]
        assert validate_rules(rules) == []

    def test_drops_year_rule_when_metadata_missing(self):
        rules = from_album({"Name": "X", "Genres": ["Jazz"]})
        # No ProductionYear → no year rule.
        assert {"field": "year", "op": "between", "value": [0, 0]} not in rules["rules"]
        ops = [r["op"] for r in rules["rules"]]
        assert "between" not in ops

    def test_excludes_seed_album(self):
        items = [
            _item_full("a", album="Homogenic", genres=["Electronic"], year=1997),
            _item_full("b", album="Post", genres=["Electronic"], year=1995),
            _item_full("c", album="Vespertine", genres=["Electronic"], year=2001),
            _item_full("d", album="OK Computer", genres=["Rock"], year=1997),
        ]
        out = refine_items(items, from_album(self._ALBUM))
        ids = {it["Id"] for it in out}
        # b survives (Electronic + 1995, not Homogenic).
        # a fails (it IS Homogenic).
        # c fails (year 2001 outside 1994-2000).
        # d fails (genre Rock).
        assert ids == {"b"}


class TestFromGenre:
    """{Genre} Discoveries: genre + play_count=0 + date_added recent."""

    def test_validates(self):
        assert validate_rules(from_genre("Jazz")) == []

    def test_rule_shape(self):
        rules = from_genre("Jazz")
        assert rules["rules"] == [
            {"field": "genre", "op": "equals", "value": "Jazz"},
            {"field": "play_count", "op": "equals", "value": 0},
            {"field": "date_added", "op": "in_the_last", "value": 90},
        ]
        assert rules["limit"] == 50
        assert rules["sort"] == "date_added"
        assert rules["sort_desc"] is True

    def test_filters_to_unplayed_recent_additions(self):
        items = [
            _item_full("recent_unplayed", genres=["Jazz"], play_count=0),
            _item_full("recent_played", genres=["Jazz"], play_count=5),
            _item_full("wrong_genre", genres=["Rock"], play_count=0),
        ]
        # Stamp DateCreated for the date_added filter — items default
        # without it, the schema treats missing dates as fails.
        for it in items:
            it["DateCreated"] = _iso_days_ago(10)
        out = refine_items(items, from_genre("Jazz"))
        assert [it["Id"] for it in out] == ["recent_unplayed"]


class TestFromTrack:
    """More like Track: genre + year window, random, cap 30."""

    _TRACK = {
        "Name": "Seven Stars",
        "Genres": ["Electronic"],
        "ProductionYear": 2007,
    }

    def test_validates(self):
        assert validate_rules(from_track(self._TRACK)) == []

    def test_rule_shape_from_item(self):
        rules = from_track(self._TRACK)
        assert rules["rules"] == [
            {"field": "genre", "op": "equals", "value": "Electronic"},
            {"field": "year", "op": "between", "value": [2004, 2010]},
        ]
        assert rules["limit"] == 30
        assert rules["sort"] == "random"

    def test_empty_when_no_metadata(self):
        # Plain string fallback → no genre, no year → empty rules.
        # The editor opens; the user fills it in.
        rules = from_track("Seven Stars")
        assert rules["rules"] == []
        assert validate_rules(rules) == []


