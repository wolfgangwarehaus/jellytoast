"""Artist top songs (both providers) + OpenSubsonic classical metadata.

- Subsonic ``getTopSongs`` goes by artist id only when the server
  advertises ``topSongsByArtistId`` (Navidrome 0.64+); otherwise by name.
- ``_extensions()`` is cached per server URL; plain Subsonic (error) is
  cached as "none", a network error is not.
- ``_adapt_song`` lifts ``works`` / ``movements`` into ``Work`` /
  ``Movement``; ``classical_work_line`` formats them for Now Playing.
Pure logic — stubs ``_request`` / ``_get``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from jellytoast.providers.jellyfin import JellyfinProvider
from jellytoast.providers.subsonic import SubsonicError, SubsonicProvider
from jellytoast.ui_helpers import classical_work_line


def _subsonic(extensions=None, top=None):
    p = SubsonicProvider()
    p._server_url = "http://nd.local"
    calls = []

    def fake_request(method, params=None, **_kw):
        calls.append((method, dict(params or {})))
        if method == "getOpenSubsonicExtensions":
            if isinstance(extensions, Exception):
                raise extensions
            return {
                "openSubsonicExtensions": [{"name": n, "versions": [1]} for n in extensions or []]
            }
        if method == "getTopSongs":
            return {"topSongs": {"song": top or []}}
        raise AssertionError(method)

    p._request = fake_request
    return p, calls


class TestSubsonicTopSongs:
    def test_by_id_when_extension_advertised(self):
        p, calls = _subsonic(["topSongsByArtistId"], [{"id": "s1", "title": "Hit"}])
        out = p.get_artist_top_songs("ar1", "Some Artist", 5)
        assert [s["Id"] for s in out] == ["s1"]
        assert calls[-1] == ("getTopSongs", {"count": 5, "id": "ar1"})

    def test_by_name_without_extension(self):
        p, calls = _subsonic(["songLyrics"])
        p.get_artist_top_songs("ar1", "Some Artist", 5)
        assert calls[-1] == ("getTopSongs", {"count": 5, "artist": "Some Artist"})

    def test_no_id_extension_and_no_name_skips_request(self):
        p, calls = _subsonic([])
        assert p.get_artist_top_songs("ar1", "") == []
        assert all(m != "getTopSongs" for m, _ in calls)

    def test_extensions_cached_per_server_url(self):
        p, calls = _subsonic(["topSongsByArtistId"])
        p._extensions()
        p._extensions()
        assert [m for m, _ in calls].count("getOpenSubsonicExtensions") == 1
        p._server_url = "http://other.local"
        p._extensions()
        assert [m for m, _ in calls].count("getOpenSubsonicExtensions") == 2

    def test_plain_subsonic_error_is_cached_as_none(self):
        p, calls = _subsonic(SubsonicError(0, "unknown method"))
        assert p._extensions() == {}
        p._extensions()
        assert [m for m, _ in calls].count("getOpenSubsonicExtensions") == 1

    def test_network_error_is_not_cached(self):
        p, calls = _subsonic(requests.ConnectionError("down"))
        assert p._extensions() == {}
        p._extensions()
        assert [m for m, _ in calls].count("getOpenSubsonicExtensions") == 2


class TestJellyfinTopSongs:
    def test_users_most_played_tracks_by_artist(self):
        prov = JellyfinProvider.__new__(JellyfinProvider)
        prov.api = MagicMock(user_id="u1")
        prov.api._get.return_value = {"Items": [{"Id": "t1"}]}
        assert prov.get_artist_top_songs("ar1", "", 5) == [{"Id": "t1"}]
        path, params = prov.api._get.call_args.args
        assert path == "/Items"
        assert params["ArtistIds"] == "ar1"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["Filters"] == "IsPlayed"
        assert params["SortBy"].startswith("PlayCount")
        assert params["SortOrder"].startswith("Descending")
        assert params["Limit"] == 5

    def test_error_is_empty(self):
        prov = JellyfinProvider.__new__(JellyfinProvider)
        prov.api = MagicMock(user_id="u1")
        prov.api._get.side_effect = RuntimeError("boom")
        assert prov.get_artist_top_songs("ar1") == []


class TestAdaptSongClassical:
    def test_lifts_first_work_and_movement(self):
        item = SubsonicProvider._adapt_song(
            {
                "id": "s1",
                "title": "Andante con moto",
                "works": [{"name": "Symphony No. 5 in C minor, Op. 67"}],
                "movements": [{"name": "Andante con moto", "number": 2, "count": 4}],
            }
        )
        assert item["Work"] == "Symphony No. 5 in C minor, Op. 67"
        assert item["Movement"] == {"Name": "Andante con moto", "Number": 2, "Count": 4}

    def test_absent_fields_are_empty(self):
        item = SubsonicProvider._adapt_song({"id": "s1", "title": "x"})
        assert item["Work"] == "" and item["Movement"] == {}


class TestClassicalWorkLine:
    WORK = "Symphony No. 5 in C minor, Op. 67"
    MV = {"Name": "Andante con moto", "Number": 2, "Count": 4}

    def test_work_and_movement(self):
        line = classical_work_line({"Work": self.WORK, "Movement": self.MV}, "Track 2")
        assert line == f"{self.WORK} · II. Andante con moto"

    def test_title_naming_the_movement_keeps_its_position(self):
        line = classical_work_line({"Work": self.WORK, "Movement": self.MV}, "Andante con moto")
        assert line == f"{self.WORK} · Movement II of IV"

    def test_title_already_holding_everything_is_empty(self):
        title = f"{self.WORK}: Andante con moto"
        mv = {"Name": "Andante con moto"}  # no number → nothing to add
        assert classical_work_line({"Work": self.WORK, "Movement": mv}, title) == ""

    @pytest.mark.parametrize("item", [{}, {"Work": "", "Movement": {}}])
    def test_non_classical_is_empty(self, item):
        assert classical_work_line(item, "Song") == ""

    def test_bad_numbers_degrade_to_name(self):
        mv = {"Name": "Allegro", "Number": "x", "Count": None}
        assert classical_work_line({"Movement": mv}, "t") == "Allegro"
