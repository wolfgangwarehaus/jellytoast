"""Tests for SubsonicProvider's seeded-radio methods.

Mocks the HTTP layer at ``SubsonicProvider._request`` so we can inspect
the path + params each method sends, and assert the adapter-shaped
return values without hitting a real server. Mirrors the
``test_jellyfin_api.py`` style: pure logic, no network, no Qt.
"""

import pytest

from modules.providers.subsonic import SubsonicProvider, SubsonicError


def _provider():
    """Bare SubsonicProvider with credentials populated enough that the
    seeded-radio methods don't short-circuit on the auth check. The
    methods under test never inspect credentials themselves — they just
    pass through to ``_request`` — but constructing one needs a sane
    settings object, which conftest's QStandardPaths-test-mode provides.
    """
    p = SubsonicProvider()
    p._username = "tester"
    p._password = "secret"
    p._server_url = "http://example.local"
    return p


class _Recorder:
    """Captures the ``(path, params)`` of every ``_request`` call and
    returns a canned response. Lets a single test exercise both the
    URL formation and the parsing of the response shape."""

    def __init__(self, response):
        self.calls = []
        self.response = response

    def __call__(self, path, params=None, server_url=None):
        self.calls.append((path, params or {}))
        return self.response


# ── get_similar_songs ────────────────────────────────────────────────────


class TestGetSimilarSongs:
    def test_calls_getSimilarSongs2_with_id_and_count(self, monkeypatch):
        rec = _Recorder(
            {
                "similarSongs2": {"song": [{"id": "s1", "title": "Track 1"}]},
            }
        )
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        result = p.get_similar_songs("seed-id", count=25)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "getSimilarSongs2"
        assert params == {"id": "seed-id", "count": 25}
        assert len(result) == 1
        assert result[0]["Id"] == "s1"
        assert result[0]["Type"] == "Audio"

    def test_default_count_is_50(self, monkeypatch):
        rec = _Recorder({"similarSongs2": {"song": []}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p.get_similar_songs("seed-id")
        assert rec.calls[0][1]["count"] == 50

    def test_empty_response_returns_empty_list(self, monkeypatch):
        # Server says "no similar tracks" (small library, new artist).
        rec = _Recorder({"similarSongs2": {}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        assert p.get_similar_songs("seed-id") == []

    def test_missing_seed_id_skips_request(self, monkeypatch):
        rec = _Recorder({})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        assert p.get_similar_songs("") == []
        assert rec.calls == []

    def test_subsonic_error_returns_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise SubsonicError(70, "Data not found")

        p = _provider()
        monkeypatch.setattr(p, "_request", boom)

        # No exception leaks; caller can fall back to random tracks.
        assert p.get_similar_songs("seed-id") == []

    def test_generic_exception_returns_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        p = _provider()
        monkeypatch.setattr(p, "_request", boom)

        assert p.get_similar_songs("seed-id") == []


# ── get_instant_mix ──────────────────────────────────────────────────────


class TestGetInstantMix:
    def test_aliases_get_similar_songs(self, monkeypatch):
        # Subsonic doesn't have a native InstantMix; the provider
        # aliases to the same getSimilarSongs2 call so call sites can
        # request a 'mix' on either provider without branching.
        rec = _Recorder(
            {
                "similarSongs2": {"song": [{"id": "s1", "title": "Track 1"}]},
            }
        )
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        result = p.get_instant_mix("seed-id", count=10)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "getSimilarSongs2"
        assert params == {"id": "seed-id", "count": 10}
        assert len(result) == 1
        assert result[0]["Id"] == "s1"

    def test_empty_seed_id_short_circuits(self, monkeypatch):
        rec = _Recorder({})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        assert p.get_instant_mix("") == []
        assert rec.calls == []

    def test_error_path_returns_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        p = _provider()
        monkeypatch.setattr(p, "_request", boom)

        assert p.get_instant_mix("seed-id") == []


# ── get_genre_radio ──────────────────────────────────────────────────────


class TestGetGenreRadio:
    def test_calls_getSongsByGenre_with_name_and_count(self, monkeypatch):
        rec = _Recorder(
            {
                "songsByGenre": {
                    "song": [
                        {"id": "s1", "title": "Track 1", "genre": "Jazz"},
                        {"id": "s2", "title": "Track 2", "genre": "Jazz"},
                    ]
                },
            }
        )
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        result = p.get_genre_radio("Jazz", count=30)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "getSongsByGenre"
        assert params == {"genre": "Jazz", "count": 30}
        assert [r["Id"] for r in result] == ["s1", "s2"]
        assert result[0]["Type"] == "Audio"

    def test_default_count_is_50(self, monkeypatch):
        rec = _Recorder({"songsByGenre": {"song": []}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p.get_genre_radio("Rock")
        assert rec.calls[0][1]["count"] == 50

    def test_empty_genre_name_skips_request(self, monkeypatch):
        rec = _Recorder({})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        assert p.get_genre_radio("") == []
        assert rec.calls == []

    def test_empty_response_returns_empty(self, monkeypatch):
        rec = _Recorder({"songsByGenre": {}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        assert p.get_genre_radio("Jazz") == []

    def test_error_path_returns_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise SubsonicError(50, "Server error")

        p = _provider()
        monkeypatch.setattr(p, "_request", boom)

        assert p.get_genre_radio("Jazz") == []

    def test_song_adapter_shape(self, monkeypatch):
        """Genre-radio responses use the same _adapt_song path as every
        other Subsonic song fetch, so durations are ticks-encoded and
        artist data lands where the views expect it."""
        rec = _Recorder(
            {
                "songsByGenre": {
                    "song": [
                        {
                            "id": "s1",
                            "title": "Foo",
                            "artist": "Bar",
                            "album": "Baz",
                            "albumId": "alb1",
                            "duration": 180,
                            "track": 3,
                            "year": 2020,
                            "genre": "Rock",
                            "suffix": "flac",
                        }
                    ]
                },
            }
        )
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        out = p.get_genre_radio("Rock", count=1)
        item = out[0]
        assert item["Name"] == "Foo"
        assert item["Album"] == "Baz"
        assert item["AlbumId"] == "alb1"
        assert item["RunTimeTicks"] == 180 * 10_000_000
        assert item["IndexNumber"] == 3
        assert item["Container"] == "flac"
