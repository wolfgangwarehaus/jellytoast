"""An artist's albums follow the top-bar library selection.

Found on august's Navidrome (2026-10-26): the artist page listed three
"LUX" albums, two of them single Discovery-library downloads of one track,
while the Albums grid (scoped to Music Library) showed one. Subsonic
getArtist ignores ``musicFolderId``, so the providers drop what a scoped
lookup places in a library the user switched off. Pure logic: stubbed
requests, no network / Qt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jellytoast import library_selection as ls
from jellytoast.providers.subsonic import SubsonicProvider


@pytest.fixture(autouse=True)
def _clean_selection_state():
    yield
    ls.reset_after_server_change()


def _libraries(selected):
    ls.set_available_libraries(
        [{"Id": "1", "Name": "Music Library"}, {"Id": "2", "Name": "Discovery"}]
    )
    ls.set_selected_ids(selected)


_ARTIST = {
    "artist": {
        "id": "ar",
        "name": "ROSALÍA",
        "album": [
            {"id": "lux", "name": "LUX", "songCount": 18},
            {"id": "lux-single", "name": "LUX", "songCount": 1},
            {"id": "motomami", "name": "MOTOMAMI", "songCount": 16},
        ],
    }
}


def _subsonic(found_in_discovery):
    p = SubsonicProvider()
    calls = []

    def _request(path, params=None, server_url=None):
        calls.append((path, params or {}))
        if path == "getArtist":
            return _ARTIST
        assert path == "search3" and params["musicFolderId"] == "2"
        assert params["query"] == "ROSALÍA"
        return {"searchResult3": {"album": [{"id": i} for i in found_in_discovery]}}

    p._request = _request
    return p, calls


def _names(albums):
    return [(a["Id"], a["Name"]) for a in albums]


def test_excluded_ids(isolated_settings):
    assert ls.excluded_ids() == []  # libraries not known yet
    _libraries(["1"])
    assert ls.excluded_ids() == ["2"]
    ls.set_selected_ids(["1", "2"])  # everything = 'all'
    assert ls.excluded_ids() == []


def test_subsonic_drops_albums_from_a_switched_off_library(isolated_settings):
    _libraries(["1"])
    p, _ = _subsonic(["lux-single"])
    assert _names(p.get_artist_albums("ar")) == [("lux", "LUX"), ("motomami", "MOTOMAMI")]


def test_subsonic_all_libraries_makes_no_extra_request(isolated_settings):
    _libraries([])
    p, calls = _subsonic([])
    assert len(p.get_artist_albums("ar")) == 3
    assert [c[0] for c in calls] == ["getArtist"]


def test_subsonic_keeps_everything_when_the_lookup_fails(isolated_settings):
    _libraries(["1"])
    p = SubsonicProvider()

    def _request(path, params=None, server_url=None):
        if path == "getArtist":
            return _ARTIST
        raise RuntimeError("search down")

    p._request = _request
    assert len(p.get_artist_albums("ar")) == 3


def test_jellyfin_drops_albums_from_a_switched_off_library(isolated_settings):
    from jellytoast.providers.jellyfin import JellyfinProvider

    _libraries(["1"])
    p = JellyfinProvider.__new__(JellyfinProvider)
    p.api = MagicMock()
    p.api.user_id = "u"
    p.api.get_artist_albums.return_value = [{"Id": "a"}, {"Id": "b"}]
    p.api._get.return_value = {"Items": [{"Id": "b"}]}
    assert p.get_artist_albums("ar") == [{"Id": "a"}]
    path, params = p.api._get.call_args.args
    assert path == "/Items" and params["ParentId"] == "2" and params["AlbumArtistIds"] == "ar"
