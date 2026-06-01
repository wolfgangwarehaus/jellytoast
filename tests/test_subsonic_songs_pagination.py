"""SubsonicProvider all-songs pagination (#10).

Subsonic has no dedicated "list all songs" endpoint. The Songs view used
to back its default (non-genre) branch with ``getRandomSongs``, which
ignores the offset and re-rolls an overlapping random batch on every page
— so a >500-track library paginated forever and appended duplicate rows.

``_get_songs`` now uses ``search3`` with an empty query + ``songOffset``,
which paginates deterministically, and falls back to a SINGLE random page
only when an empty-query search3 returns nothing at offset 0 (legacy
servers that reject empty-query match-all). Mocks the HTTP layer at
``SubsonicProvider._request`` — pure logic, no network, no Qt. Mirrors the
``test_subsonic_seeded_radio.py`` style.
"""

from __future__ import annotations

from modules.providers.subsonic import SubsonicProvider


def _provider():
    p = SubsonicProvider()
    p._username = "tester"
    p._password = "secret"
    p._server_url = "http://example.local"
    return p


class _Recorder:
    """Captures the ``(path, params)`` of every ``_request`` and returns a
    single canned response."""

    def __init__(self, response):
        self.calls = []
        self.response = response

    def __call__(self, path, params=None, server_url=None):
        self.calls.append((path, params or {}))
        return self.response


class _PathRouter:
    """Returns a canned response keyed by request path; records calls.
    Lets one test exercise the search3 → getRandomSongs fallback path."""

    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    def __call__(self, path, params=None, server_url=None):
        self.calls.append((path, params or {}))
        return self.by_path.get(path, {})


class TestGetSongsSearch3:
    def test_uses_search3_with_offset(self, monkeypatch):
        rec = _Recorder(
            {"searchResult3": {"song": [
                {"id": "s1", "title": "A"}, {"id": "s2", "title": "B"},
            ]}}
        )
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        out = p._get_songs(parent_id="", limit=500, start_index=500, genre_id="")

        assert len(rec.calls) == 1
        path, params = rec.calls[0]
        assert path == "search3"
        # The offset is forwarded — the whole point of #10.
        assert params["songOffset"] == 500
        assert params["query"] == ""
        assert params["songCount"] == 500
        assert params["albumCount"] == 0 and params["artistCount"] == 0
        assert "musicFolderId" not in params
        assert [it["Id"] for it in out["Items"]] == ["s1", "s2"]

    def test_forwards_musicfolder_and_caps_count(self, monkeypatch):
        # offset > 0 + empty result so the empty-query fallback doesn't fire.
        rec = _Recorder({"searchResult3": {"song": []}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p._get_songs(parent_id="mf7", limit=1000, start_index=500, genre_id="")

        _, params = rec.calls[0]
        assert params["musicFolderId"] == "mf7"
        assert params["songCount"] == 500  # capped at the Subsonic max

    def test_search3_error_returns_empty(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network")

        p = _provider()
        monkeypatch.setattr(p, "_request", boom)

        out = p._get_songs(parent_id="", limit=500, start_index=0, genre_id="")
        assert out == {"Items": [], "TotalRecordCount": 0}


class TestGetSongsRandomFallback:
    def test_empty_search3_falls_back_to_random_at_offset_zero(self, monkeypatch):
        router = _PathRouter({
            "search3": {"searchResult3": {}},  # no 'song' bucket → empty
            "getRandomSongs": {"randomSongs": {"song": [
                {"id": "r1", "title": "Rand"},
            ]}},
        })
        p = _provider()
        monkeypatch.setattr(p, "_request", router)

        out = p._get_songs(parent_id="", limit=500, start_index=0, genre_id="")

        # Tried search3 first, then fell back to a single random page.
        assert [c[0] for c in router.calls] == ["search3", "getRandomSongs"]
        assert [it["Id"] for it in out["Items"]] == ["r1"]

    def test_empty_search3_past_first_page_returns_empty(self, monkeypatch):
        # On a legacy server the fallback runs ONCE (offset 0). Every later
        # page must come back empty so songs_view's tail-stop trips and the
        # background cascade terminates — no infinite random re-roll.
        router = _PathRouter({
            "search3": {"searchResult3": {}},
            "getRandomSongs": {"randomSongs": {"song": [{"id": "r1"}]}},
        })
        p = _provider()
        monkeypatch.setattr(p, "_request", router)

        out = p._get_songs(parent_id="", limit=500, start_index=500, genre_id="")

        assert [c[0] for c in router.calls] == ["search3"]  # no fallback past page 1
        assert out["Items"] == []


class TestGetSongsGenreUnchanged:
    def test_genre_branch_uses_getSongsByGenre_with_offset(self, monkeypatch):
        # Regression guard: the genre branch already forwarded its offset
        # correctly and must not be disturbed by the search3 rework.
        rec = _Recorder({"songsByGenre": {"song": [{"id": "g1", "title": "G"}]}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        out = p._get_songs(parent_id="", limit=500, start_index=100, genre_id="Rock")

        path, params = rec.calls[0]
        assert path == "getSongsByGenre"
        assert params == {"genre": "Rock", "count": 500, "offset": 100}
        assert [it["Id"] for it in out["Items"]] == ["g1"]


class TestRandomRadioHelperUntouched:
    def test_get_random_audio_items_still_one_shot_random(self, monkeypatch):
        # The radio-refill helper is INTENTIONALLY a one-shot random draw
        # (queue_manager refill) — it must keep using getRandomSongs with no
        # offset. Locks the scope of the #10 fix so it isn't widened by
        # mistake.
        rec = _Recorder({"randomSongs": {"song": [{"id": "r1"}, {"id": "r2"}]}})
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        out = p.get_random_audio_items("", limit=50)

        path, params = rec.calls[0]
        assert path == "getRandomSongs"
        assert params == {"size": 50}
        assert len(out) == 2
