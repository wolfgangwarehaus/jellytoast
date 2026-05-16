"""Tests for JellyfinProvider's seeded-radio methods.

Mocks the underlying ``JellyfinAPI._get`` so the tests can assert the
URL path + params each method sends, plus the parsing of Jellyfin's
``{"Items": [...]}`` envelope. No real HTTP, no Qt event loop.
"""

import pytest

from modules.providers.jellyfin import JellyfinProvider


def _provider():
    """JellyfinProvider whose underlying API has a fake user_id so the
    test assertions can match exact param dicts."""
    p = JellyfinProvider()
    p.api.user_id = "user-abc"
    p.api.server_url = "http://example.local"
    return p


class _Recorder:
    """Captures ``(path, params)`` from a JellyfinAPI._get call and
    returns a canned envelope."""

    def __init__(self, response):
        self.calls = []
        self.response = response

    def __call__(self, path, params=None):
        self.calls.append((path, params or {}))
        return self.response


# ── get_similar_songs ────────────────────────────────────────────────────


class TestGetSimilarSongs:
    def test_calls_similar_endpoint_with_limit(self, monkeypatch):
        rec = _Recorder({"Items": [{"Id": "t1", "Name": "Track 1"}]})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        result = p.get_similar_songs("seed-id", count=25)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "/Items/seed-id/Similar"
        assert params["Limit"] == 25
        assert params["UserId"] == "user-abc"
        assert "Fields" in params
        assert result == [{"Id": "t1", "Name": "Track 1"}]

    def test_default_count_is_50(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        p.get_similar_songs("seed-id")
        assert rec.calls[0][1]["Limit"] == 50

    def test_missing_seed_id_skips_request(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_similar_songs("") == []
        assert rec.calls == []

    def test_empty_envelope_returns_empty(self, monkeypatch):
        # Jellyfin returns an empty Items list when nothing is similar.
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_similar_songs("seed-id") == []

    def test_missing_items_key_returns_empty(self, monkeypatch):
        # Defensive — some Jellyfin error paths return {} with no Items.
        rec = _Recorder({})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_similar_songs("seed-id") == []

    def test_http_error_returns_empty(self, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError("HTTP 500")

        p = _provider()
        monkeypatch.setattr(p.api, "_get", boom)

        assert p.get_similar_songs("seed-id") == []


# ── get_instant_mix ──────────────────────────────────────────────────────


class TestGetInstantMix:
    def test_calls_instant_mix_endpoint_with_limit(self, monkeypatch):
        rec = _Recorder({"Items": [{"Id": "t1"}, {"Id": "t2"}]})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        result = p.get_instant_mix("seed-id", count=10)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "/Items/seed-id/InstantMix"
        assert params["Limit"] == 10
        assert params["UserId"] == "user-abc"
        assert "Fields" in params
        assert [r["Id"] for r in result] == ["t1", "t2"]

    def test_default_count_is_50(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        p.get_instant_mix("seed-id")
        assert rec.calls[0][1]["Limit"] == 50

    def test_missing_seed_id_skips_request(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_instant_mix("") == []
        assert rec.calls == []

    def test_error_path_returns_empty(self, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError("network")

        p = _provider()
        monkeypatch.setattr(p.api, "_get", boom)

        assert p.get_instant_mix("seed-id") == []


# ── get_genre_radio ──────────────────────────────────────────────────────


class TestGetGenreRadio:
    def test_calls_items_endpoint_with_filter_and_random_sort(self, monkeypatch):
        rec = _Recorder({"Items": [{"Id": "t1"}, {"Id": "t2"}]})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        result = p.get_genre_radio("Jazz", count=30)

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "/Users/user-abc/Items"
        assert params["Genres"] == "Jazz"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["SortBy"] == "Random"
        assert params["Limit"] == 30
        assert params["Recursive"] is True
        assert [r["Id"] for r in result] == ["t1", "t2"]

    def test_default_count_is_50(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        p.get_genre_radio("Rock")
        assert rec.calls[0][1]["Limit"] == 50

    def test_empty_genre_name_skips_request(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_genre_radio("") == []
        assert rec.calls == []

    def test_empty_envelope_returns_empty(self, monkeypatch):
        rec = _Recorder({"Items": []})
        p = _provider()
        monkeypatch.setattr(p.api, "_get", rec)

        assert p.get_genre_radio("Jazz") == []

    def test_error_path_returns_empty(self, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError("HTTP 500")

        p = _provider()
        monkeypatch.setattr(p.api, "_get", boom)

        assert p.get_genre_radio("Jazz") == []
