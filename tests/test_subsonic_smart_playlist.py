"""Tests for SubsonicProvider.query_items — the smart-playlist stub.

The stub translates a small initial rule subset into Subsonic
``getSongsByGenre`` and ``getAlbumList2?type=byYear`` calls. These
tests stub ``SubsonicProvider._request`` to capture the params we
emit and feed back canned responses so the adapt + sort + limit
pipeline is exercised end-to-end without touching the network.
"""

from __future__ import annotations

import pytest

from modules.providers.subsonic import SubsonicProvider


@pytest.fixture
def provider(monkeypatch):
    """A SubsonicProvider with ``_request`` stubbed to a recording fake.

    ``provider.calls`` accumulates ``(path, params)`` tuples; tests
    inspect them to verify URL formation. ``provider.responses`` is a
    dict the test sets up to map a Subsonic endpoint name to its
    canned JSON return shape.
    """
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
    # get_album_tracks fans out from byYear; route it through _request
    # too so the test stub's response map covers everything.
    return p


def _song(id_, title="Song", year=2020):
    return {
        "id": id_,
        "title": title,
        "year": year,
        "artist": "Artist",
        "album": "Album",
        "duration": 180,
        "suffix": "flac",
    }


class TestGenreEquals:
    def test_translates_to_getsongsbygenre(self, provider):
        provider.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [_song("s1", "A"), _song("s2", "B")],
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Electronic"}],
            }
        )
        assert len(out) == 2
        assert out[0]["Id"] == "s1"
        # URL formation check.
        assert provider.calls[0][0] == "getSongsByGenre"
        assert provider.calls[0][1]["genre"] == "Electronic"

    def test_limit_truncates_results(self, provider):
        provider.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [_song(f"s{i}") for i in range(10)],
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Pop"}],
                "limit": 3,
            }
        )
        assert len(out) == 3


class TestYearOps:
    def test_year_equals_uses_byyear_range(self, provider):
        provider.responses["getAlbumList2"] = {
            "albumList2": {"album": [{"id": "alb1", "name": "X", "year": 2007}]},
        }
        provider.responses["getAlbum"] = {
            "album": {"id": "alb1", "song": [_song("s1", year=2007)]},
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": 2007}],
            }
        )
        # byYear params should be fromYear=toYear=2007.
        first = provider.calls[0]
        assert first[0] == "getAlbumList2"
        assert first[1]["type"] == "byYear"
        assert first[1]["fromYear"] == 2007
        assert first[1]["toYear"] == 2007
        assert out and out[0]["Id"] == "s1"

    def test_year_greater_than_widens_upper_bound(self, provider):
        provider.responses["getAlbumList2"] = {
            "albumList2": {"album": []},
        }
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "greater_than", "value": 2000}],
            }
        )
        params = provider.calls[0][1]
        assert params["fromYear"] == 2001  # strict
        assert params["toYear"] == 9999

    def test_year_between_normalizes_order(self, provider):
        provider.responses["getAlbumList2"] = {
            "albumList2": {"album": []},
        }
        provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "between", "value": [2010, 2005]}],
            }
        )
        params = provider.calls[0][1]
        assert params["fromYear"] == 2005
        assert params["toYear"] == 2010


class TestUnsupportedCombos:
    def test_play_count_rule_falls_back_to_broad_fetch(self, provider):
        # play_count has no Subsonic server mapping; the evaluator
        # falls back to a broad alphabeticalByArtist fetch and then
        # refines client-side. Stub returns no albums so the query
        # still returns []; we only assert the broad fetch path was
        # taken.
        provider.responses["getAlbumList2"] = {
            "albumList2": {"album": []},
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "play_count", "op": "greater_than", "value": 5}],
            }
        )
        assert out == []
        # Broad fetch hits alphabeticalByArtist.
        assert provider.calls[0][0] == "getAlbumList2"
        assert provider.calls[0][1]["type"] == "alphabeticalByArtist"

    def test_invalid_rule_raises_valueerror(self, provider):
        # Validator runs first; an unknown field surfaces as ValueError
        # before any provider call.
        with pytest.raises(ValueError):
            provider.query_items(
                {
                    "match": "all",
                    "rules": [{"field": "bpm", "op": "equals", "value": 120}],
                }
            )

    def test_empty_rule_list_returns_empty(self, provider):
        # No rules → no calls, empty list back.
        out = provider.query_items({"match": "all", "rules": []})
        assert out == []
        assert provider.calls == []
