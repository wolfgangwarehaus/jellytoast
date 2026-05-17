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

from modules.providers.jellyfin import (
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


def _audio(id_, name="Track", year=2020, artists=None,
           album_artist="AA", album="Al", genres=None,
           play_count=0, rating=None):
    return {
        "Id": id_, "Name": name, "Type": "Audio",
        "ProductionYear": year,
        "Artists": artists or ["AA"],
        "AlbumArtist": album_artist,
        "Album": album,
        "Genres": genres or [],
        "CommunityRating": rating,
        "UserData": {"PlayCount": play_count, "IsFavorite": False},
    }


class TestQueryFormation:
    def test_genre_equals_sets_genres_param(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "genre", "op": "equals",
                       "value": "Electronic"}],
        })
        path, params = provider.calls[0]
        assert path == "/Users/u1/Items"
        assert params["Genres"] == "Electronic"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["Recursive"] == "true"

    def test_year_equals_sets_years_param(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "year", "op": "equals", "value": 2007}],
        })
        params = provider.calls[0][1]
        assert params["Years"] == "2007"

    def test_year_between_expands_to_year_list(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "year", "op": "between",
                       "value": [2000, 2003]}],
        })
        params = provider.calls[0][1]
        # Sorted, comma-joined, inclusive.
        assert params["Years"] == "2000,2001,2002,2003"

    def test_play_count_gt_uses_min_user_play_count(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "play_count", "op": "greater_than",
                       "value": 5}],
        })
        params = provider.calls[0][1]
        # Strict gt = X means Min...= X+1 (inclusive on server side).
        assert params["MinUserPlayCount"] == 6

    def test_rating_gt_uses_min_community_rating(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "rating", "op": "greater_than",
                       "value": 3}],
        })
        params = provider.calls[0][1]
        # Float bump so "gt 3" excludes exactly 3.
        assert params["MinCommunityRating"] > 3
        assert params["MinCommunityRating"] < 3.01

    def test_limit_param_propagated(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "genre", "op": "equals", "value": "R"}],
            "limit": 42,
        })
        params = provider.calls[0][1]
        assert params["Limit"] == 42

    def test_sort_translates_to_sortby(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "genre", "op": "equals", "value": "R"}],
            "sort": "play_count",
            "sort_desc": True,
        })
        params = provider.calls[0][1]
        assert params["SortBy"] == "PlayCount"
        assert params["SortOrder"] == "Descending"

    def test_default_sort_is_sortname_ascending(self, provider):
        provider.query_items({
            "match": "all",
            "rules": [{"field": "genre", "op": "equals", "value": "R"}],
        })
        params = provider.calls[0][1]
        assert params["SortBy"] == "SortName"
        assert params["SortOrder"] == "Ascending"


class TestClientSideRefine:
    def test_year_less_than_filters_response(self, provider):
        provider.next_response = {"Items": [
            _audio("a", year=1995),
            _audio("b", year=2005),  # excluded by <2000
            _audio("c", year=1999),
        ]}
        out = provider.query_items({
            "match": "all",
            "rules": [{"field": "year", "op": "less_than", "value": 2000}],
        })
        assert {it["Id"] for it in out} == {"a", "c"}

    def test_artist_contains_filters_response(self, provider):
        provider.next_response = {"Items": [
            _audio("a", artists=["Daft Punk"]),
            _audio("b", artists=["Air"]),
            _audio("c", artists=["Justice"]),
        ]}
        out = provider.query_items({
            "match": "all",
            "rules": [{"field": "artist", "op": "contains", "value": "ai"}],
        })
        ids = {it["Id"] for it in out}
        # "ai" matches "Air" (case-insensitive).
        assert "b" in ids
        assert "a" not in ids and "c" not in ids


class TestUnsupportedInput:
    def test_invalid_rules_raise_valueerror(self, provider):
        with pytest.raises(ValueError):
            provider.query_items({
                "match": "all",
                "rules": [{"field": "bpm", "op": "equals", "value": 120}],
            })

    def test_empty_rule_set_returns_empty_without_call(self, provider):
        out = provider.query_items({"match": "all", "rules": []})
        assert out == []
        assert provider.calls == []


class TestMatchAny:
    def test_union_across_rules(self, provider, monkeypatch):
        # Two rules + match=any -> two separate /Items calls; union
        # of returned items returned to the caller.
        responses = iter([
            {"Items": [_audio("a"), _audio("b")]},
            {"Items": [_audio("b"), _audio("c")]},  # 'b' is dup
        ])

        def _fake_get(path, params=None):
            provider.calls.append((path, dict(params or {})))
            return next(responses)

        monkeypatch.setattr(provider.api, "_get", _fake_get)
        out = provider.query_items({
            "match": "any",
            "rules": [
                {"field": "genre", "op": "equals", "value": "Rock"},
                {"field": "genre", "op": "equals", "value": "Pop"},
            ],
        })
        # Three unique items across both legs.
        assert {it["Id"] for it in out} == {"a", "b", "c"}
        assert len(provider.calls) == 2


class TestBuildJfQueryDirect:
    def test_year_less_than_does_not_set_years_param(self):
        # less_than goes to a client-side refine (not enumerative),
        # so neither Years= nor the satisfied-list gets the rule.
        rule = {"field": "year", "op": "less_than", "value": 1990}
        params, satisfied = _build_jf_query(
            [rule], sort=None, sort_desc=False,
        )
        assert "Years" not in params
        assert satisfied == []   # rule will need a Python refine pass
