"""Tests for the smart-playlist multi-rule evaluator.

Two layers under test:

1. ``jellytoast.providers.smart_rule_eval.refine_items`` — pure Python
   pass that applies a full rule set (AND/OR), sort, and limit to
   an already-fetched item list. No network, no providers — just
   data shape.
2. Multi-rule provider integration on both Subsonic and Jellyfin.
   The providers fetch a candidate set from the server (one query
   per rule for OR, one query for the most selective rule for AND)
   then refine in Python.

Adapted-item shape: providers always emit Jellyfin-style dicts
(``Id``, ``Name``, ``ProductionYear``, ``Genres``, ``Album``,
``Artists``, ``AlbumArtist``, ``UserData.PlayCount``,
``CommunityRating``). The eval module reads through those keys.
"""

from __future__ import annotations

import pytest

from jellytoast.providers.smart_rule_eval import (
    matches_rule,
    refine_items,
    sort_items,
)


def _item(
    id_,
    *,
    name="Track",
    year=2020,
    genres=None,
    artists=None,
    album_artist="AA",
    album="Al",
    play_count=0,
    rating=None,
    is_favorite=False,
):
    """Build an adapted (Jellyfin-shape) audio item dict."""
    return {
        "Id": id_,
        "Name": name,
        "Type": "Audio",
        "ProductionYear": year,
        "Genres": list(genres or []),
        "Artists": list(artists or [album_artist]),
        "AlbumArtist": album_artist,
        "Album": album,
        "CommunityRating": rating,
        "UserData": {"PlayCount": play_count, "IsFavorite": is_favorite},
    }


# ─────────────────────────────────────────────────────────────────────
# refine_items — operator coverage
# ─────────────────────────────────────────────────────────────────────


class TestEqualsOperator:
    def test_genre_equals_picks_only_matching(self):
        items = [
            _item("a", genres=["Rock"]),
            _item("b", genres=["Pop"]),
            _item("c", genres=["Rock", "Indie"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Rock"}],
            },
        )
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_year_equals_int_compare(self):
        items = [_item("a", year=2007), _item("b", year=2008)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 2007}],
            },
        )
        assert [it["Id"] for it in out] == ["a"]

    def test_year_equals_string_compare(self):
        # #243: a Subsonic ProductionYear can come back as a string; equals
        # must coerce both sides (like the range ops) instead of raw ==,
        # which made '2007' == 2007 False and dropped matching tracks.
        out = refine_items(
            [_item("a", year="2007"), _item("b", year="2008")],
            {"match": "all", "rules": [{"field": "year", "op": "equals", "value": 2007}]},
        )
        assert [it["Id"] for it in out] == ["a"]


class TestInTheLastBoundary:
    def test_date_only_n_days_ago_is_included(self):
        # #170: a date-only timestamp (parses to midnight) from exactly N
        # days ago must match "in the last N days" no matter the current
        # time-of-day. Pre-fix the cutoff kept now()'s time, so midnight-
        # N-days-ago fell just before it and was wrongly excluded.
        import datetime as dt

        from jellytoast.providers.smart_rule_eval import _in_the_last

        now = dt.datetime(2026, 5, 30, 14, 30)  # afternoon
        added = dt.datetime(2026, 5, 23, 0, 0)  # midnight, exactly 7 days prior
        assert _in_the_last(added, 7, now=now) is True
        # Sanity: 8 days prior is still outside a 7-day window.
        assert _in_the_last(dt.datetime(2026, 5, 22, 0, 0), 7, now=now) is False

    def test_artist_equals_matches_album_artist_or_artists_list(self):
        items = [
            _item("a", album_artist="Feist", artists=["Feist"]),
            _item("b", album_artist="Various", artists=["Feist", "Air"]),
            _item("c", album_artist="Air", artists=["Air"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "equals", "value": "Feist"}],
            },
        )
        assert {it["Id"] for it in out} == {"a", "b"}

    def test_album_equals(self):
        items = [_item("a", album="Reminder"), _item("b", album="Pleasure")]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "album", "op": "equals", "value": "Reminder"}],
            },
        )
        assert [it["Id"] for it in out] == ["a"]


class TestNotEqualsOperator:
    def test_genre_not_equals_excludes_matching(self):
        items = [
            _item("a", genres=["Rock"]),
            _item("b", genres=["Pop"]),
            _item("c", genres=[]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "not_equals", "value": "Rock"}],
            },
        )
        # "c" has no genres at all — it doesn't include Rock so it stays.
        assert {it["Id"] for it in out} == {"b", "c"}


class TestContainsOperator:
    def test_artist_contains_case_insensitive(self):
        items = [
            _item("a", artists=["Daft Punk"]),
            _item("b", artists=["Air"]),
            _item("c", artists=["Justice"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "contains", "value": "AI"}],
            },
        )
        # "AI" matches "Air" (case-insensitive substring).
        assert {it["Id"] for it in out} == {"b"}

    def test_album_contains_substring(self):
        items = [
            _item("a", album="The Reminder"),
            _item("b", album="Pleasure"),
            _item("c", album="Remind Me"),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "album", "op": "contains", "value": "remind"}],
            },
        )
        assert {it["Id"] for it in out} == {"a", "c"}


class TestGreaterLessThan:
    def test_year_greater_than_strict(self):
        items = [_item(str(y), year=y) for y in (1999, 2000, 2001, 2002)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "greater_than", "value": 2000}],
            },
        )
        assert {it["Id"] for it in out} == {"2001", "2002"}

    def test_year_less_than_strict(self):
        items = [_item(str(y), year=y) for y in (1995, 1999, 2000)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "less_than", "value": 2000}],
            },
        )
        assert {it["Id"] for it in out} == {"1995", "1999"}

    def test_play_count_greater_than(self):
        items = [
            _item("a", play_count=2),
            _item("b", play_count=10),
            _item("c", play_count=5),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "play_count", "op": "greater_than", "value": 5}],
            },
        )
        assert {it["Id"] for it in out} == {"b"}

    def test_play_count_less_than_excludes_zero_played_via_unrated(self):
        # play_count=0 is a real value; less_than 1 includes it.
        items = [
            _item("a", play_count=0),
            _item("b", play_count=1),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "play_count", "op": "less_than", "value": 1}],
            },
        )
        assert {it["Id"] for it in out} == {"a"}

    def test_rating_greater_than_treats_none_as_zero(self):
        items = [
            _item("a", rating=4),
            _item("b", rating=2),
            _item("c", rating=None),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "rating", "op": "greater_than", "value": 3}],
            },
        )
        assert {it["Id"] for it in out} == {"a"}


class TestBetweenOperator:
    def test_year_between_inclusive(self):
        items = [_item(str(y), year=y) for y in (1999, 2000, 2005, 2010, 2011)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "between", "value": [2000, 2010]}],
            },
        )
        assert {it["Id"] for it in out} == {"2000", "2005", "2010"}

    def test_year_between_normalizes_order(self):
        items = [_item(str(y), year=y) for y in (2000, 2005, 2010)]
        # Reversed bounds should still match the same range.
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "between", "value": [2010, 2000]}],
            },
        )
        assert {it["Id"] for it in out} == {"2000", "2005", "2010"}


# ─────────────────────────────────────────────────────────────────────
# refine_items — match semantics
# ─────────────────────────────────────────────────────────────────────


class TestMatchSemantics:
    def test_match_all_is_intersection(self):
        items = [
            _item("a", year=2007, genres=["Rock"]),
            _item("b", year=2007, genres=["Pop"]),
            _item("c", year=2010, genres=["Rock"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "year", "op": "equals", "value": 2007},
                ],
            },
        )
        assert {it["Id"] for it in out} == {"a"}

    def test_match_any_is_union(self):
        items = [
            _item("a", year=2007, genres=["Rock"]),
            _item("b", year=2007, genres=["Pop"]),
            _item("c", year=2010, genres=["Rock"]),
            _item("d", year=2015, genres=["Jazz"]),
        ]
        out = refine_items(
            items,
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "year", "op": "equals", "value": 2007},
                ],
            },
        )
        # a (both), b (year), c (genre) match; d matches neither.
        assert {it["Id"] for it in out} == {"a", "b", "c"}

    def test_match_default_is_all(self):
        # match key absent → AND semantics.
        items = [
            _item("a", year=2007, genres=["Rock"]),
            _item("b", year=2008, genres=["Rock"]),
        ]
        out = refine_items(
            items,
            {
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "year", "op": "equals", "value": 2007},
                ],
            },
        )
        assert {it["Id"] for it in out} == {"a"}


# ─────────────────────────────────────────────────────────────────────
# refine_items — edge cases
# ─────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_rules_list_returns_input_unchanged(self):
        items = [_item("a"), _item("b")]
        out = refine_items(items, {"match": "all", "rules": []})
        assert [it["Id"] for it in out] == ["a", "b"]

    def test_limit_zero_returns_empty(self):
        items = [_item("a"), _item("b")]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [],
                "limit": 0,
            },
        )
        assert out == []

    def test_limit_truncates_after_filter(self):
        items = [_item(str(i), year=2007) for i in range(10)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 2007}],
                "limit": 3,
            },
        )
        assert len(out) == 3

    def test_limit_none_keeps_all(self):
        items = [_item(str(i)) for i in range(50)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [],
                "limit": None,
            },
        )
        assert len(out) == 50

    def test_sort_by_missing_field_does_not_crash(self):
        # Items missing the sort field land last; the call returns
        # all of them rather than raising.
        items = [
            _item("a", year=2007),
            {"Id": "b", "Name": "x"},  # no ProductionYear key
            _item("c", year=2005),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [],
                "sort": "year",
            },
        )
        # c (2005), a (2007), b (None) — None sorts last.
        ids = [it["Id"] for it in out]
        assert ids[0] == "c"
        assert ids[1] == "a"
        assert ids[2] == "b"

    def test_sort_descending_reverses_order(self):
        items = [_item(str(y), year=y) for y in (2000, 2005, 2010)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [],
                "sort": "year",
                "sort_desc": True,
            },
        )
        assert [it["Id"] for it in out] == ["2010", "2005", "2000"]

    def test_empty_input_returns_empty(self):
        out = refine_items(
            [],
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 2007}],
            },
        )
        assert out == []


class TestSortItemsHelper:
    def test_sort_with_none_field_is_noop(self):
        items = [_item("a"), _item("b")]
        out = sort_items(items, None, False)
        assert [it["Id"] for it in out] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────
# is_favorite field
# ─────────────────────────────────────────────────────────────────────


class TestIsFavoriteField:
    def test_equals_true_keeps_only_favorites(self):
        items = [
            _item("a", is_favorite=True),
            _item("b", is_favorite=False),
            _item("c", is_favorite=True),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            },
        )
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_equals_false_keeps_only_non_favorites(self):
        items = [
            _item("a", is_favorite=True),
            _item("b", is_favorite=False),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": False}],
            },
        )
        assert {it["Id"] for it in out} == {"b"}

    def test_missing_userdata_reads_as_not_favorite(self):
        # Item with no UserData at all — is_favorite resolves to False
        # gracefully rather than raising.
        item = {"Id": "x", "Name": "x"}
        assert (
            matches_rule(item, {"field": "is_favorite", "op": "equals", "value": True})
            is False
        )
        assert (
            matches_rule(item, {"field": "is_favorite", "op": "equals", "value": False})
            is True
        )

    def test_missing_isfavorite_key_reads_as_false(self):
        item = {"Id": "x", "Name": "x", "UserData": {"PlayCount": 3}}
        assert (
            matches_rule(item, {"field": "is_favorite", "op": "equals", "value": True})
            is False
        )


# ─────────────────────────────────────────────────────────────────────
# starts_with / ends_with operators
# ─────────────────────────────────────────────────────────────────────


class TestStartsWithOperator:
    def test_artist_starts_with_positive(self):
        items = [
            _item("a", artists=["The Beatles"]),
            _item("b", artists=["Radiohead"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "starts_with", "value": "the"}],
            },
        )
        assert {it["Id"] for it in out} == {"a"}

    def test_album_starts_with_negative(self):
        items = [
            _item("a", album="Abbey Road"),
            _item("b", album="OK Computer"),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "album", "op": "starts_with", "value": "z"}],
            },
        )
        assert out == []

    def test_starts_with_missing_field_is_false(self):
        # No Album key at all → no match, no crash.
        item = {"Id": "x", "Name": "x"}
        assert (
            matches_rule(item, {"field": "album", "op": "starts_with", "value": "a"})
            is False
        )


class TestEndsWithOperator:
    def test_artist_ends_with_positive(self):
        items = [
            _item("a", artists=["Arcade Fire"]),
            _item("b", artists=["LCD Soundsystem"]),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "ends_with", "value": "fire"}],
            },
        )
        assert {it["Id"] for it in out} == {"a"}

    def test_album_ends_with_negative(self):
        items = [
            _item("a", album="Reminder"),
            _item("b", album="Pleasure"),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [{"field": "album", "op": "ends_with", "value": "xyz"}],
            },
        )
        assert out == []

    def test_ends_with_missing_field_is_false(self):
        item = {"Id": "x", "Name": "x"}
        assert (
            matches_rule(item, {"field": "artist", "op": "ends_with", "value": "z"})
            is False
        )


# ─────────────────────────────────────────────────────────────────────
# sort: random
# ─────────────────────────────────────────────────────────────────────


class TestRandomSort:
    def test_random_returns_all_items(self):
        items = [_item(str(i)) for i in range(20)]
        out = sort_items(items, "random", False)
        assert len(out) == len(items)
        assert {it["Id"] for it in out} == {it["Id"] for it in items}

    def test_random_with_seeded_rng_is_reproducible(self):
        import random as _random

        items = [_item(str(i)) for i in range(20)]
        a = sort_items(items, "random", False, rng=_random.Random(42))
        b = sort_items(items, "random", False, rng=_random.Random(42))
        assert [it["Id"] for it in a] == [it["Id"] for it in b]

    def test_random_actually_shuffles(self):
        # With a fixed seed the order should differ from input for a
        # large enough list (vanishingly unlikely to be identity).
        import random as _random

        items = [_item(str(i)) for i in range(50)]
        out = sort_items(items, "random", False, rng=_random.Random(7))
        assert [it["Id"] for it in out] != [it["Id"] for it in items]

    def test_random_via_refine_items_respects_limit(self):
        items = [_item(str(i)) for i in range(30)]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [],
                "sort": "random",
                "limit": 5,
            },
        )
        assert len(out) == 5
        # Every result is a real item from the input.
        input_ids = {it["Id"] for it in items}
        assert all(it["Id"] in input_ids for it in out)

    def test_random_does_not_mutate_input(self):
        items = [_item(str(i)) for i in range(10)]
        original = [it["Id"] for it in items]
        sort_items(items, "random", False)
        assert [it["Id"] for it in items] == original


class TestMatchesRuleHelper:
    def test_unknown_op_returns_false(self):
        item = _item("a", year=2007)
        assert matches_rule(item, {"field": "year", "op": "regex", "value": ".*"}) is False

    def test_between_with_wrong_value_shape_returns_false(self):
        item = _item("a", year=2007)
        # Malformed between (single value, not a pair) — schema would
        # catch upstream, but the predicate is defensive too.
        assert matches_rule(item, {"field": "year", "op": "between", "value": 2007}) is False


# ─────────────────────────────────────────────────────────────────────
# Subsonic multi-rule integration
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def subsonic_provider(monkeypatch):
    from jellytoast.providers.subsonic import SubsonicProvider

    p = SubsonicProvider()
    p._username = "test"
    p._password = "test"
    p._server_url = "https://example.invalid"
    p.calls = []
    p.responses = {}

    def _fake_request(path, params=None, server_url=None):
        p.calls.append((path, dict(params or {})))
        return p.responses.get(path, {})

    monkeypatch.setattr(p, "_request", _fake_request)
    return p


def _sub_song(
    id_, *, title="Song", year=2020, artist="Artist", album="Album", genre=None, play_count=0
):
    s = {
        "id": id_,
        "title": title,
        "year": year,
        "artist": artist,
        "album": album,
        "duration": 180,
        "suffix": "flac",
        "playCount": play_count,
    }
    if genre:
        s["genre"] = genre
    return s


class TestSubsonicMultiRule:
    def test_genre_and_year_uses_getsongsbygenre_then_refines(
        self,
        subsonic_provider,
    ):
        # genre=Pop is the first server-mappable rule; year>2000 is
        # the in-Python refine.
        p = subsonic_provider
        p.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [
                    _sub_song("s1", year=1995, genre="Pop"),
                    _sub_song("s2", year=2005, genre="Pop"),
                    _sub_song("s3", year=2010, genre="Pop"),
                ],
            },
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Pop"},
                    {"field": "year", "op": "greater_than", "value": 2000},
                ],
            }
        )
        # First call must be getSongsByGenre with genre=Pop.
        assert p.calls[0][0] == "getSongsByGenre"
        assert p.calls[0][1]["genre"] == "Pop"
        # Refine drops 1995, keeps 2005+2010.
        assert {it["Id"] for it in out} == {"s2", "s3"}

    def test_fallback_to_broad_fetch_when_no_mappable_rule(
        self,
        subsonic_provider,
    ):
        # play_count + rating have no Subsonic server mapping; the
        # broad alphabeticalByArtist fetch is taken instead.
        p = subsonic_provider
        p.responses["getAlbumList2"] = {
            "albumList2": {
                "album": [{"id": "alb1", "name": "X", "year": 2010}],
            },
        }
        p.responses["getAlbum"] = {
            "album": {
                "id": "alb1",
                "song": [
                    _sub_song("s1", play_count=10),
                    _sub_song("s2", play_count=2),
                ],
            },
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "play_count", "op": "greater_than", "value": 5},
                ],
            }
        )
        # Broad fetch type=alphabeticalByArtist.
        assert p.calls[0][0] == "getAlbumList2"
        assert p.calls[0][1]["type"] == "alphabeticalByArtist"
        # Refine keeps only play_count > 5.
        assert [it["Id"] for it in out] == ["s1"]

    def test_match_any_union_across_genre_rules(self, subsonic_provider):
        # Two genre rules, OR semantics → two getSongsByGenre calls,
        # union of returned songs.
        p = subsonic_provider
        responses = iter(
            [
                {
                    "songsByGenre": {
                        "song": [
                            _sub_song("s1", genre="Rock"),
                            _sub_song("s2", genre="Rock"),
                        ]
                    }
                },
                {
                    "songsByGenre": {
                        "song": [
                            _sub_song("s2", genre="Rock"),  # dup
                            _sub_song("s3", genre="Pop"),
                        ]
                    }
                },
            ]
        )

        def _fake_request(path, params=None, server_url=None):
            p.calls.append((path, dict(params or {})))
            if path == "getSongsByGenre":
                return next(responses)
            return {}

        p._request = _fake_request
        out = p.query_items(
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "genre", "op": "equals", "value": "Pop"},
                ],
            }
        )
        assert {it["Id"] for it in out} == {"s1", "s2", "s3"}
        # Two server queries fired.
        sg_calls = [c for c in p.calls if c[0] == "getSongsByGenre"]
        assert len(sg_calls) == 2

    def test_match_all_intersection_vs_match_any_union(
        self,
        subsonic_provider,
    ):
        # Same songs, two rules; verify AND vs OR diverge as expected.
        p = subsonic_provider
        p.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [
                    _sub_song("s1", year=2007, genre="Pop"),
                    _sub_song("s2", year=2020, genre="Pop"),
                ],
            },
        }
        # match=all: genre=Pop (server) AND year=2007 (refine) → s1.
        and_out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Pop"},
                    {"field": "year", "op": "equals", "value": 2007},
                ],
            }
        )
        assert {it["Id"] for it in and_out} == {"s1"}

    def test_single_rule_still_works(self, subsonic_provider):
        # Regression: single-rule single-call path unchanged.
        p = subsonic_provider
        p.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [_sub_song("s1", genre="Electronic")],
            },
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Electronic"}],
            }
        )
        assert len(out) == 1
        assert out[0]["Id"] == "s1"


# ─────────────────────────────────────────────────────────────────────
# Jellyfin multi-rule integration
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def jellyfin_provider(monkeypatch):
    from jellytoast.providers.jellyfin import JellyfinProvider

    p = JellyfinProvider()
    p.api.user_id = "u1"
    p.calls = []
    p.next_response = {"Items": []}

    def _fake_get(path, params=None):
        p.calls.append((path, dict(params or {})))
        return p.next_response

    monkeypatch.setattr(p.api, "_get", _fake_get)
    return p


def _jf_audio(
    id_,
    *,
    name="Track",
    year=2020,
    artists=None,
    album_artist="AA",
    album="Al",
    genres=None,
    play_count=0,
    rating=None,
):
    return {
        "Id": id_,
        "Name": name,
        "Type": "Audio",
        "ProductionYear": year,
        "Artists": list(artists or [album_artist]),
        "AlbumArtist": album_artist,
        "Album": album,
        "Genres": list(genres or []),
        "CommunityRating": rating,
        "UserData": {"PlayCount": play_count, "IsFavorite": False},
    }


class TestJellyfinMultiRule:
    def test_genre_and_play_count_pushes_genres_refines_playcount(
        self,
        jellyfin_provider,
    ):
        # genre=Electronic + play_count>5 → /Items with Genres=Electronic,
        # MinUserPlayCount=6 server-side (since play_count>5 is mappable
        # too).  Both rules are pushed; refine is a no-op on the data.
        p = jellyfin_provider
        p.next_response = {
            "Items": [
                _jf_audio("a", genres=["Electronic"], play_count=10),
                _jf_audio("b", genres=["Electronic"], play_count=7),
            ]
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Electronic"},
                    {"field": "play_count", "op": "greater_than", "value": 5},
                ],
            }
        )
        assert len(p.calls) == 1
        params = p.calls[0][1]
        assert params["Genres"] == "Electronic"
        assert params["MinUserPlayCount"] == 6
        assert {it["Id"] for it in out} == {"a", "b"}

    def test_genre_and_artist_refines_artist_in_python(
        self,
        jellyfin_provider,
    ):
        # artist has no server mapping in v1 → /Items pushes Genres=Rock
        # then refine_items filters by artist=Air in Python.
        p = jellyfin_provider
        p.next_response = {
            "Items": [
                _jf_audio("a", genres=["Rock"], artists=["Air"]),
                _jf_audio("b", genres=["Rock"], artists=["Wire"]),
                _jf_audio("c", genres=["Rock"], artists=["Air"]),
            ]
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "artist", "op": "equals", "value": "Air"},
                ],
            }
        )
        params = p.calls[0][1]
        assert params["Genres"] == "Rock"
        # A client-refine rule means the fetch pages the server-filtered
        # set (Limit = page size + StartIndex) rather than pushing the
        # playlist's own limit — refine_items must see every item before
        # it filters, so the page Limit is the paging window, not a cap.
        assert params["Limit"] == 500
        assert params["StartIndex"] == 0
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_match_any_unions_two_genre_queries(self, jellyfin_provider):
        p = jellyfin_provider
        responses = iter(
            [
                {"Items": [_jf_audio("a", genres=["Rock"]), _jf_audio("b", genres=["Rock"])]},
                {
                    "Items": [
                        _jf_audio("b", genres=["Rock"]),  # dup
                        _jf_audio("c", genres=["Pop"]),
                    ]
                },
            ]
        )

        def _fake_get(path, params=None):
            p.calls.append((path, dict(params or {})))
            return next(responses)

        p.api._get = _fake_get
        out = p.query_items(
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "genre", "op": "equals", "value": "Pop"},
                ],
            }
        )
        assert {it["Id"] for it in out} == {"a", "b", "c"}
        assert len(p.calls) == 2

    def test_match_all_intersection_with_year_refine(
        self,
        jellyfin_provider,
    ):
        # year>2000 isn't pushed (enumerative); refine drops 1999.
        p = jellyfin_provider
        p.next_response = {
            "Items": [
                _jf_audio("a", year=1999, genres=["Rock"]),
                _jf_audio("b", year=2005, genres=["Rock"]),
                _jf_audio("c", year=2010, genres=["Rock"]),
            ]
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "year", "op": "greater_than", "value": 2000},
                ],
            }
        )
        # Genres pushed; Years not pushed (greater_than refines).
        params = p.calls[0][1]
        assert params["Genres"] == "Rock"
        assert "Years" not in params
        assert {it["Id"] for it in out} == {"b", "c"}

    def test_single_rule_path_still_works(self, jellyfin_provider):
        # Regression: single-rule path unchanged.
        p = jellyfin_provider
        p.next_response = {
            "Items": [
                _jf_audio("a", genres=["Electronic"]),
            ]
        }
        out = p.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Electronic"}],
            }
        )
        assert [it["Id"] for it in out] == ["a"]

    def test_limit_truncates_match_any(self, jellyfin_provider):
        p = jellyfin_provider
        responses = iter(
            [
                {"Items": [_jf_audio(f"a{i}", genres=["Rock"]) for i in range(3)]},
                {"Items": [_jf_audio(f"b{i}", genres=["Pop"]) for i in range(3)]},
            ]
        )

        def _fake_get(path, params=None):
            p.calls.append((path, dict(params or {})))
            return next(responses)

        p.api._get = _fake_get
        out = p.query_items(
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "genre", "op": "equals", "value": "Pop"},
                ],
                "limit": 4,
            }
        )
        assert len(out) == 4


# ─────────────────────────────────────────────────────────────────────
# Schema v2 — date fields (date_added / last_played)
# ─────────────────────────────────────────────────────────────────────


from datetime import datetime, timedelta, timezone  # noqa: E402


def _now():
    return datetime.now()


def _iso_days_ago(days: int) -> str:
    """ISO date string for N days ago — for date_added (DateCreated)."""
    return (_now() - timedelta(days=days)).date().isoformat()


def _iso_dt_days_ago(days: int) -> str:
    """ISO datetime string for N days ago — for last_played."""
    return (_now() - timedelta(days=days)).isoformat()


def _dated_item(id_, *, date_added=None, last_played=None, play_count=0):
    """Adapted item carrying optional DateCreated / LastPlayedDate."""
    item = {
        "Id": id_,
        "Name": f"Track {id_}",
        "Type": "Audio",
        "ProductionYear": 2020,
        "Genres": [],
        "Artists": ["AA"],
        "AlbumArtist": "AA",
        "Album": "Al",
        "UserData": {"PlayCount": play_count, "IsFavorite": False},
    }
    if date_added is not None:
        item["DateCreated"] = date_added
    if last_played is not None:
        item["UserData"]["LastPlayedDate"] = last_played
    return item


class TestInTheLastOperator:
    def test_date_added_in_window_matches(self):
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        assert matches_rule(_dated_item("a", date_added=_iso_days_ago(10)), rule)

    def test_date_added_outside_window_excluded(self):
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        assert not matches_rule(_dated_item("a", date_added=_iso_days_ago(60)), rule)

    def test_boundary_day_is_inclusive(self):
        # A track dated exactly N days ago still counts as "in the
        # last N days" — the cutoff comparison is >= and floors to the
        # start of the day. Build the edge in the SAME naive-UTC frame
        # _in_the_last uses (backend item dates are UTC) — using local
        # _now() here is wrong: west of UTC, near UTC-midnight, the local
        # edge lands a calendar day before the UTC-floored cutoff and the
        # row is wrongly excluded. Two minutes inside the boundary so
        # clock drift between cutoff computation and the value can't flip it.
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        edge = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=30)
            + timedelta(minutes=2)
        ).isoformat()
        assert matches_rule(_dated_item("a", date_added=edge), rule)

    def test_missing_date_added_non_match(self):
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        assert not matches_rule(_dated_item("a"), rule)

    def test_none_date_added_non_match(self):
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        assert not matches_rule(_dated_item("a", date_added=None), rule)

    def test_unparseable_date_non_match(self):
        rule = {"field": "date_added", "op": "in_the_last", "value": 30}
        assert not matches_rule(_dated_item("a", date_added="garbage"), rule)

    def test_last_played_uses_userdata(self):
        rule = {"field": "last_played", "op": "in_the_last", "value": 30}
        assert matches_rule(_dated_item("a", last_played=_iso_dt_days_ago(5)), rule)
        assert not matches_rule(
            _dated_item("b", last_played=_iso_dt_days_ago(90)), rule
        )

    def test_last_played_missing_non_match(self):
        # A never-played track has no LastPlayedDate — it must simply
        # not match a last_played rule rather than crash.
        rule = {"field": "last_played", "op": "in_the_last", "value": 30}
        assert not matches_rule(_dated_item("a"), rule)


class TestBeforeAfterOperators:
    def test_before_matches_earlier_date(self):
        rule = {"field": "date_added", "op": "before", "value": "2026-01-01"}
        assert matches_rule(_dated_item("a", date_added="2025-06-01"), rule)

    def test_before_excludes_later_date(self):
        rule = {"field": "date_added", "op": "before", "value": "2026-01-01"}
        assert not matches_rule(_dated_item("a", date_added="2026-06-01"), rule)

    def test_before_is_strict(self):
        # Equal date is NOT before — strict inequality.
        rule = {"field": "date_added", "op": "before", "value": "2026-01-01"}
        assert not matches_rule(_dated_item("a", date_added="2026-01-01"), rule)

    def test_after_matches_later_date(self):
        rule = {"field": "last_played", "op": "after", "value": "2026-01-01"}
        assert matches_rule(
            _dated_item("a", last_played="2026-03-01T08:00:00"), rule
        )

    def test_after_excludes_earlier_date(self):
        rule = {"field": "last_played", "op": "after", "value": "2026-01-01"}
        assert not matches_rule(
            _dated_item("a", last_played="2025-03-01T08:00:00"), rule
        )

    def test_after_is_strict(self):
        rule = {"field": "date_added", "op": "after", "value": "2026-01-01"}
        assert not matches_rule(_dated_item("a", date_added="2026-01-01"), rule)

    def test_before_missing_date_non_match(self):
        rule = {"field": "date_added", "op": "before", "value": "2026-01-01"}
        assert not matches_rule(_dated_item("a"), rule)

    def test_after_missing_date_non_match(self):
        rule = {"field": "last_played", "op": "after", "value": "2026-01-01"}
        assert not matches_rule(_dated_item("a"), rule)

    def test_zulu_suffix_parses(self):
        rule = {"field": "date_added", "op": "after", "value": "2025-12-31"}
        assert matches_rule(
            _dated_item("a", date_added="2026-01-15T10:00:00Z"), rule
        )


class TestDateFieldRefineIntegration:
    def test_in_the_last_filters_through_refine_items(self):
        items = [
            _dated_item("old", date_added=_iso_days_ago(100)),
            _dated_item("new", date_added=_iso_days_ago(5)),
            _dated_item("undated"),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [
                    {"field": "date_added", "op": "in_the_last", "value": 30}
                ],
            },
        )
        assert [it["Id"] for it in out] == ["new"]

    def test_combined_play_count_and_last_played(self):
        items = [
            _dated_item("recent", play_count=20, last_played=_iso_dt_days_ago(10)),
            _dated_item("forgotten", play_count=20, last_played=_iso_dt_days_ago(200)),
            _dated_item("rare", play_count=1, last_played=_iso_dt_days_ago(200)),
        ]
        out = refine_items(
            items,
            {
                "match": "all",
                "rules": [
                    {"field": "play_count", "op": "greater_than", "value": 5},
                    {"field": "last_played", "op": "before", "value": _iso_days_ago(90)},
                ],
            },
        )
        assert [it["Id"] for it in out] == ["forgotten"]


class TestDateFieldSort:
    def test_sort_by_date_added_descending(self):
        items = [
            _dated_item("mid", date_added=_iso_days_ago(50)),
            _dated_item("new", date_added=_iso_days_ago(2)),
            _dated_item("old", date_added=_iso_days_ago(300)),
        ]
        out = sort_items(items, "date_added", sort_desc=True)
        assert [it["Id"] for it in out] == ["new", "mid", "old"]

    def test_sort_by_last_played_ascending(self):
        items = [
            _dated_item("a", last_played=_iso_dt_days_ago(10)),
            _dated_item("b", last_played=_iso_dt_days_ago(200)),
        ]
        out = sort_items(items, "last_played", sort_desc=False)
        assert [it["Id"] for it in out] == ["b", "a"]

    def test_undated_items_sort_last(self):
        # Items with no date sort last regardless of direction.
        items = [
            _dated_item("undated"),
            _dated_item("dated", date_added=_iso_days_ago(10)),
        ]
        asc = sort_items(items, "date_added", sort_desc=False)
        desc = sort_items(items, "date_added", sort_desc=True)
        assert asc[-1]["Id"] == "undated"
        assert desc[-1]["Id"] == "undated"


# ── Date-bounded Jellyfin fetch (schema v2) ──────────────────────────────────
# A date_added / last_played rule has no server-side filter, so the whole
# library is fetched and refined in Python. The fetch pages (one unbounded
# request times out on a large library) and, for "recent" rules, sorts by
# the date field and stops paging once it crosses the cutoff.


class TestRecentDateBound:
    def test_in_the_last_yields_bound(self):
        from jellytoast.providers.jellyfin import _recent_date_bound

        bound = _recent_date_bound(
            [{"field": "date_added", "op": "in_the_last", "value": 30}]
        )
        assert bound is not None
        field, jf_key, cutoff = bound
        assert (field, jf_key) == ("date_added", "DateCreated")
        # The cutoff lives in the client filter's naive-UTC, start-of-day
        # frame (#153 follow-up) — not local time — so the server-side
        # paging early-exit agrees with the client _in_the_last filter.
        expected = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        assert cutoff == expected
        assert cutoff.tzinfo is None

    def test_after_yields_bound(self):
        from jellytoast.providers.jellyfin import _recent_date_bound

        bound = _recent_date_bound(
            [{"field": "last_played", "op": "after", "value": "2026-01-01"}]
        )
        assert bound is not None
        field, jf_key, cutoff = bound
        assert (field, jf_key) == ("last_played", "DatePlayed")
        assert (cutoff.year, cutoff.month, cutoff.day) == (2026, 1, 1)

    def test_before_is_not_a_recent_bound(self):
        from jellytoast.providers.jellyfin import _recent_date_bound

        # `before` selects OLD items — not a recent bound, full paging.
        assert (
            _recent_date_bound(
                [{"field": "date_added", "op": "before", "value": "2020-01-01"}]
            )
            is None
        )

    def test_non_date_rule_yields_no_bound(self):
        from jellytoast.providers.jellyfin import _recent_date_bound

        assert (
            _recent_date_bound([{"field": "genre", "op": "equals", "value": "Rock"}])
            is None
        )

    def test_tightest_cutoff_wins(self):
        from jellytoast.providers.jellyfin import _recent_date_bound

        bound = _recent_date_bound(
            [
                {"field": "date_added", "op": "in_the_last", "value": 180},
                {"field": "date_added", "op": "in_the_last", "value": 7},
            ]
        )
        # The 7-day cutoff is later (tighter) than the 180-day one, in the
        # naive-UTC start-of-day frame.
        expected = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        assert bound[2] == expected

    def test_in_the_last_cutoff_in_client_utc_frame_not_local(self):
        # #153 follow-up: the server-side paging early-exit cutoff must live
        # in the SAME naive-UTC, start-of-day frame as the client
        # _in_the_last filter. While the client frame moved to UTC, this
        # bound was still computed with local datetime.now() — so for a user
        # east of UTC the page loop could stop before fetching items the
        # filter would keep (silent under-fetch on paginating libraries).
        # This test is timezone-independent: it fails if the cutoff is in
        # local time OR keeps now()'s time-of-day (unfloored).
        from jellytoast.providers.jellyfin import _recent_date_bound

        bound = _recent_date_bound(
            [{"field": "date_added", "op": "in_the_last", "value": 7}]
        )
        assert bound is not None
        cutoff = bound[2]
        assert cutoff.tzinfo is None
        expected = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        assert cutoff == expected


class TestJellyfinDatePagedFetch:
    def _install_paging_get(self, p, all_items):
        """Wire the fake provider's _get to serve StartIndex/Limit pages."""

        def paged_get(path, params=None):
            params = params or {}
            p.calls.append((path, dict(params)))
            start = int(params.get("StartIndex", 0))
            lim = int(params.get("Limit", 500))
            return {"Items": all_items[start : start + lim]}

        p.api._get = paged_get

    def test_recent_rule_stops_paging_at_cutoff(self, jellyfin_provider):
        # 1200 items, one day apart. `in_the_last 100` matches the
        # ~100 newest; page 0 (500 items) already crosses the cutoff,
        # so paging stops after a single request.
        p = jellyfin_provider
        items = [
            _dated_item(f"t{i}", date_added=_iso_days_ago(i)) for i in range(1200)
        ]
        self._install_paging_get(p, items)

        out = p.query_items(
            {
                "match": "all",
                "rules": [{"field": "date_added", "op": "in_the_last", "value": 100}],
            }
        )
        assert len(p.calls) == 1
        first = p.calls[0][1]
        assert first["SortBy"] == "DateCreated"
        assert first["SortOrder"] == "Descending"
        assert 90 <= len(out) <= 110

    def test_broad_window_pages_the_whole_library(self, jellyfin_provider):
        # `in_the_last 5000` covers every item — no early exit, so the
        # fetch pages the whole 1200-item library (3 × 500-item pages).
        p = jellyfin_provider
        items = [
            _dated_item(f"t{i}", date_added=_iso_days_ago(i)) for i in range(1200)
        ]
        self._install_paging_get(p, items)

        out = p.query_items(
            {
                "match": "all",
                "rules": [{"field": "date_added", "op": "in_the_last", "value": 5000}],
            }
        )
        assert len(p.calls) == 3
        assert len(out) == 1200


class TestTimezoneNormalization:
    """#153: dates are normalized to naive-UTC (offset CONVERTED, not
    dropped) and compared against a naive-UTC now, so date rules don't
    drift by the user's UTC offset (both backends emit UTC)."""

    def test_offset_converted_to_utc(self):
        import datetime as dt

        from jellytoast.providers.smart_rule_schema import parse_iso_date

        # +08:00 midnight → 16:00 the PREVIOUS day in UTC. Pre-fix this
        # dropped the offset and returned 2026-05-20 00:00 (wrong).
        assert parse_iso_date("2026-05-20T00:00:00+08:00") == dt.datetime(2026, 5, 19, 16, 0, 0)

    def test_zulu_is_utc(self):
        import datetime as dt

        from jellytoast.providers.smart_rule_schema import parse_iso_date

        assert parse_iso_date("2026-05-20T12:00:00Z") == dt.datetime(2026, 5, 20, 12, 0, 0)

    def test_date_only_unchanged(self):
        import datetime as dt

        from jellytoast.providers.smart_rule_schema import parse_iso_date

        assert parse_iso_date("2026-05-20") == dt.datetime(2026, 5, 20, 0, 0, 0)

    def test_in_the_last_in_utc_frame(self):
        import datetime as dt

        from jellytoast.providers.smart_rule_eval import _in_the_last
        from jellytoast.providers.smart_rule_schema import parse_iso_date

        # Item stamped 23:00 UTC yesterday, now = noon UTC today → within
        # the last 1 day, independent of the test machine's local tz.
        actual = parse_iso_date("2026-05-29T23:00:00Z")
        now = dt.datetime(2026, 5, 30, 12, 0, 0)
        assert _in_the_last(actual, 1, now=now) is True
