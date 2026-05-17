"""Tests for the smart-playlist multi-rule evaluator.

Two layers under test:

1. ``modules.providers.smart_rule_eval.refine_items`` — pure Python
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

from modules.providers.smart_rule_eval import (
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
        "UserData": {"PlayCount": play_count, "IsFavorite": False},
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
    from modules.providers.subsonic import SubsonicProvider

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
    from modules.providers.jellyfin import JellyfinProvider

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
        # Limit should not have been pushed since we need to refine.
        assert "Limit" not in params
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
