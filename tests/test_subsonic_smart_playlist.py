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


def _song(id_, title="Song", year=2020, artist="Artist", album="Album", starred=None):
    s = {
        "id": id_,
        "title": title,
        "year": year,
        "artist": artist,
        "album": album,
        "duration": 180,
        "suffix": "flac",
    }
    if starred is not None:
        s["starred"] = starred
    return s


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


class TestIsFavorite:
    def test_equals_true_uses_getstarred2(self, provider):
        provider.responses["getStarred2"] = {
            "starred2": {
                "song": [
                    _song("s1", starred="2026-05-01T00:00:00"),
                    _song("s2", starred="2026-05-02T00:00:00"),
                ],
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            }
        )
        assert provider.calls[0][0] == "getStarred2"
        assert {it["Id"] for it in out} == {"s1", "s2"}

    def test_equals_true_forces_favorite_flag_even_without_starred(self, provider):
        # Some servers omit the per-song `starred` timestamp in the
        # getStarred2 payload. The provider must still mark them
        # favorites so the refine pass doesn't drop them all.
        provider.responses["getStarred2"] = {
            "starred2": {
                "song": [_song("s1")],  # no `starred` key
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            }
        )
        assert len(out) == 1
        assert out[0]["UserData"]["IsFavorite"] is True

    def test_equals_false_falls_back_to_broad_fetch(self, provider):
        # `equals False` has no native endpoint — broad fetch + refine.
        provider.responses["getAlbumList2"] = {
            "albumList2": {"album": []},
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": False}],
            }
        )
        assert out == []
        assert provider.calls[0][0] == "getAlbumList2"
        assert provider.calls[0][1]["type"] == "alphabeticalByArtist"


class TestStartsEndsWithRefine:
    def test_artist_starts_with_refines_after_genre_query(self, provider):
        # genre=Rock is server-mapped; artist starts_with refines in
        # Python over the returned songs.
        provider.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [
                    _song("s1", artist="The Cure"),
                    _song("s2", artist="Pixies"),
                    _song("s3", artist="The Smiths"),
                ],
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Rock"},
                    {"field": "artist", "op": "starts_with", "value": "the"},
                ],
            }
        )
        assert {it["Id"] for it in out} == {"s1", "s3"}

    def test_album_ends_with_refines_after_genre_query(self, provider):
        provider.responses["getSongsByGenre"] = {
            "songsByGenre": {
                "song": [
                    _song("s1", album="Acoustic Sessions"),
                    _song("s2", album="Greatest Hits"),
                ],
            },
        }
        out = provider.query_items(
            {
                "match": "all",
                "rules": [
                    {"field": "genre", "op": "equals", "value": "Pop"},
                    {"field": "album", "op": "ends_with", "value": "sessions"},
                ],
            }
        )
        assert {it["Id"] for it in out} == {"s1"}


class TestStarredFavoritesSort:
    """#581: getAlbumList2 type=starred returns server star-date order and
    ignores the requested sort, so the Favorites rail was unsorted on
    Subsonic while Jellyfin (which sends SortBy to the server) returned it
    alphabetically. _get_albums now re-sorts the starred result client-side
    to match — provider parity."""

    def _starred(self, names):
        # Server returns them out of name order (as star-date order would).
        return {"albumList2": {"album": [{"id": f"a{i}", "name": n} for i, n in enumerate(names)]}}

    def test_favorites_resorted_ascending_by_name(self, provider):
        provider.responses["getAlbumList2"] = self._starred(["Zebra", "Apple", "Mango"])
        out = provider.get_items(
            "", "MusicAlbum", 10, 0, "SortName", "Ascending", True, "", "IsFavorite"
        )
        # Issued the starred listing...
        assert provider.calls[0][0] == "getAlbumList2"
        assert provider.calls[0][1]["type"] == "starred"
        # ...and the result is alphabetical, not star-date order.
        assert [it["Name"] for it in out["Items"]] == ["Apple", "Mango", "Zebra"]

    def test_favorites_resorted_descending(self, provider):
        provider.responses["getAlbumList2"] = self._starred(["Apple", "Zebra", "Mango"])
        out = provider.get_items(
            "", "MusicAlbum", 10, 0, "SortName", "Descending", True, "", "IsFavorite"
        )
        assert [it["Name"] for it in out["Items"]] == ["Zebra", "Mango", "Apple"]

    def test_favorites_sort_strips_leading_article(self, provider):
        # "The Beatles" sorts under B, matching library_grid / Jellyfin.
        provider.responses["getAlbumList2"] = self._starred(["Cake", "The Beatles", "Air"])
        out = provider.get_items(
            "", "MusicAlbum", 10, 0, "SortName", "Ascending", True, "", "IsFavorite"
        )
        assert [it["Name"] for it in out["Items"]] == ["Air", "The Beatles", "Cake"]
