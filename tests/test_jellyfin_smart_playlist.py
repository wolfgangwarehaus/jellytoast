"""Tests for JellyfinProvider.query_items — smart-playlist stub.

These tests verify URL/param formation for each field+op pair the
v1 schema supports. ``JellyfinAPI._get`` is patched on the api
instance so no HTTP fires; ``provider.calls`` records every call so
tests can assert what parameters Jellyfin would have seen.

The client-side refine pass also runs in-process here so we exercise
both halves of the translator: server params *and* the post-fetch
filter on returned items.
"""

from __future__ import annotations

import pytest

from jellytoast.providers.jellyfin import (
    JellyfinProvider,
    _build_jf_query,
)


@pytest.fixture
def provider(monkeypatch):
    """A JellyfinProvider with the underlying ``api._get`` stubbed."""
    p = JellyfinProvider()
    p.api.user_id = "u1"
    p.calls = []
    # Test sets the next response; default is empty page.
    p.next_response = {"Items": []}

    def _fake_get(path, params=None):
        p.calls.append((path, dict(params or {})))
        return p.next_response

    monkeypatch.setattr(p.api, "_get", _fake_get)
    return p


def _audio(
    id_,
    name="Track",
    year=2020,
    artists=None,
    album_artist="AA",
    album="Al",
    genres=None,
    play_count=0,
    rating=None,
    is_favorite=False,
):
    return {
        "Id": id_,
        "Name": name,
        "Type": "Audio",
        "ProductionYear": year,
        "Artists": artists or ["AA"],
        "AlbumArtist": album_artist,
        "Album": album,
        "Genres": genres or [],
        "CommunityRating": rating,
        "UserData": {"PlayCount": play_count, "IsFavorite": is_favorite},
    }


class TestQueryFormation:
    def test_genre_equals_sets_genres_param(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Electronic"}],
            }
        )
        path, params = provider.calls[0]
        assert path == "/Users/u1/Items"
        assert params["Genres"] == "Electronic"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["Recursive"] == "true"

    def test_year_equals_sets_years_param(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 2007}],
            }
        )
        params = provider.calls[0][1]
        assert params["Years"] == "2007"

    def test_year_between_expands_to_year_list(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "between", "value": [2000, 2003]}],
            }
        )
        params = provider.calls[0][1]
        # Sorted, comma-joined, inclusive.
        assert params["Years"] == "2000,2001,2002,2003"

    def test_play_count_gt_uses_min_user_play_count(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "play_count", "op": "greater_than", "value": 5}],
            }
        )
        params = provider.calls[0][1]
        # Strict gt = X means Min...= X+1 (inclusive on server side).
        assert params["MinUserPlayCount"] == 6

    def test_rating_gt_uses_min_community_rating(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "rating", "op": "greater_than", "value": 3}],
            }
        )
        params = provider.calls[0][1]
        # Float bump so "gt 3" excludes exactly 3.
        assert params["MinCommunityRating"] > 3
        assert params["MinCommunityRating"] < 3.01

    def test_limit_param_propagated(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "R"}],
                "limit": 42,
            }
        )
        params = provider.calls[0][1]
        assert params["Limit"] == 42

    def test_sort_translates_to_sortby(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "R"}],
                "sort": "play_count",
                "sort_desc": True,
            }
        )
        params = provider.calls[0][1]
        assert params["SortBy"] == "PlayCount"
        assert params["SortOrder"] == "Descending"

    def test_default_sort_is_sortname_ascending(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "R"}],
            }
        )
        params = provider.calls[0][1]
        assert params["SortBy"] == "SortName"
        assert params["SortOrder"] == "Ascending"


class TestClientSideRefine:
    def test_year_less_than_filters_response(self, provider):
        provider.next_response = {
            "Items": [
                _audio("a", year=1995),
                _audio("b", year=2005),  # excluded by <2000
                _audio("c", year=1999),
            ]
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "less_than", "value": 2000}],
            }
        )
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_artist_contains_filters_response(self, provider):
        provider.next_response = {
            "Items": [
                _audio("a", artists=["Daft Punk"]),
                _audio("b", artists=["Air"]),
                _audio("c", artists=["Justice"]),
            ]
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "contains", "value": "ai"}],
            }
        )
        ids = {it["Id"] for it in out}
        # "ai" matches "Air" (case-insensitive).
        assert "b" in ids
        assert "a" not in ids and "c" not in ids


class TestUnsupportedInput:
    def test_invalid_rules_raise_valueerror(self, provider):
        with pytest.raises(ValueError):
            provider.query_items(
                {
                    "match": "all",
                    "rules": [{"field": "bpm", "op": "equals", "value": 120}],
                }
            )

    def test_empty_rule_set_returns_empty_without_call(self, provider):
        out = provider.query_items({"match": "all", "rules": []})
        assert out == []
        assert provider.calls == []


class TestMatchAny:
    def test_union_across_rules(self, provider, monkeypatch):
        # Two rules + match=any -> two separate /Items calls; union of
        # returned items. Each leg's items carry the genre its rule
        # filters on (what a real server returns) so client-side
        # refinement keeps them.
        responses = iter(
            [
                {"Items": [_audio("a", genres=["Rock"]), _audio("b", genres=["Rock"])]},
                {"Items": [_audio("b", genres=["Pop"]), _audio("c", genres=["Pop"])]},  # 'b' dup
            ]
        )

        def _fake_get(path, params=None):
            provider.calls.append((path, dict(params or {})))
            return next(responses)

        monkeypatch.setattr(provider.api, "_get", _fake_get)
        out = provider.query_items(
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "genre", "op": "equals", "value": "Pop"},
                ],
            }
        )
        # Three unique items across both legs.
        assert {it["Id"] for it in out} == {"a", "b", "c"}
        assert len(provider.calls) == 2

    def test_union_refines_each_leg_dropping_server_leaks(self, provider, monkeypatch):
        """#3: a leg's server filter can be partial/absent, so
        _query_jf_single may return items that DON'T satisfy the rule.
        They must be filtered client-side (matches_rule), not unioned
        blindly — else a match=any playlist fills with non-matching
        tracks. Here the Rock leg leaks a Jazz track that must be dropped."""
        responses = iter(
            [
                {"Items": [_audio("a", genres=["Rock"]), _audio("leak", genres=["Jazz"])]},
                {"Items": [_audio("c", genres=["Pop"])]},
            ]
        )

        def _fake_get(path, params=None):
            provider.calls.append((path, dict(params or {})))
            return next(responses)

        monkeypatch.setattr(provider.api, "_get", _fake_get)
        out = provider.query_items(
            {
                "match": "any",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "genre", "op": "equals", "value": "Pop"},
                ],
            }
        )
        assert {it["Id"] for it in out} == {"a", "c"}  # 'leak' (Jazz) dropped


class TestBuildJfQueryDirect:
    def test_year_less_than_does_not_set_years_param(self):
        # less_than goes to a client-side refine (not enumerative),
        # so neither Years= nor the satisfied-list gets the rule.
        rule = {"field": "year", "op": "less_than", "value": 1990}
        params, satisfied = _build_jf_query(
            [rule],
            sort=None,
            sort_desc=False,
        )
        assert "Years" not in params
        assert satisfied == []  # rule will need a Python refine pass


class TestIsFavoriteQuery:
    def test_is_favorite_true_sets_isfavorite_param(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            }
        )
        params = provider.calls[0][1]
        assert params["IsFavorite"] == "true"

    def test_is_favorite_false_sets_isfavorite_param(self, provider):
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": False}],
            }
        )
        params = provider.calls[0][1]
        assert params["IsFavorite"] == "false"

    def test_is_favorite_rule_is_satisfied_server_side(self):
        # Server enforces IsFavorite — the rule lands in `satisfied`
        # so the Python refine pass doesn't re-check it.
        rule = {"field": "is_favorite", "op": "equals", "value": True}
        params, satisfied = _build_jf_query([rule], sort=None, sort_desc=False)
        assert params["IsFavorite"] == "true"
        assert satisfied == [rule]

    def test_is_favorite_returns_server_items(self, provider):
        provider.next_response = {
            "Items": [
                _audio("a", is_favorite=True),
                _audio("b", is_favorite=True),
            ]
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            }
        )
        assert {it["Id"] for it in out} == {"a", "b"}


class TestStartsEndsWithRefine:
    def test_artist_starts_with_filters_response(self, provider):
        provider.next_response = {
            "Items": [
                _audio("a", artists=["The Cure"]),
                _audio("b", artists=["Pixies"]),
                _audio("c", artists=["The National"]),
            ]
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "starts_with", "value": "the"}],
            }
        )
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_album_ends_with_filters_response(self, provider):
        provider.next_response = {
            "Items": [
                _audio("a", album="Unplugged"),
                _audio("b", album="Studio Sessions"),
            ]
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "album", "op": "ends_with", "value": "sessions"}],
            }
        )
        assert {it["Id"] for it in out} == {"b"}

    def test_starts_with_not_pushed_server_side(self):
        # No server filter for prefix matching — the rule needs the
        # Python refine pass (not in `satisfied`).
        rule = {"field": "artist", "op": "starts_with", "value": "The"}
        params, satisfied = _build_jf_query([rule], sort=None, sort_desc=False)
        assert satisfied == []
        assert "ArtistIds" not in params
