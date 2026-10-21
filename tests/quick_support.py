"""Shared pieces for Qt Quick front end tests: a fake library behind the
provider API, and a fixture that loads the real QML against it with a
``Rig`` (jellytoast/quick/rig.py) to drive it.

    def test_something(quick):
        quick.tap("text:Album 3")
        quick.wait_for("trackList")
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

N_ALBUMS = 150
TRACKS_PER_ALBUM = 6
ARTISTS = ["Air", "Björk", "Boards of Canada", "Caribou", "Daft Punk", "Four Tet"]
GENRES = ["Ambient", "Electronic", "Pop", "Rock"]


def _album(i: int) -> Dict[str, Any]:
    artist = ARTISTS[i % len(ARTISTS)]
    return {
        "Id": f"al{i}",
        "Name": f"Album {i:03d}",
        "AlbumArtist": artist,
        "AlbumArtists": [{"Id": f"ar{i % len(ARTISTS)}", "Name": artist}],
        "ProductionYear": 1990 + i % 30,
        "Genre": GENRES[i % len(GENRES)],
        "UserData": {"IsFavorite": i % 10 == 0},
        "Type": "MusicAlbum",
    }


def _track(album: Dict[str, Any], n: int) -> Dict[str, Any]:
    return {
        "Id": f"{album['Id']}-t{n}",
        "Name": f"Track {n} of {album['Name']}",
        "IndexNumber": n,
        "ParentIndexNumber": 1,
        "RunTimeTicks": (150 + n * 10) * 10_000_000,
        "Artists": [album["AlbumArtist"]],
        "AlbumArtist": album["AlbumArtist"],
        "Album": album["Name"],
        "AlbumId": album["Id"],
        "Type": "Audio",
        "UserData": {"IsFavorite": n == 1},
    }


class FakeProvider:
    """The slice of the provider API the Quick UI uses. Records calls."""

    is_authenticated = True
    kind = "jellyfin"
    password = "secret"  # authenticate() accepts only this

    def __init__(self):
        self.albums = [_album(i) for i in range(N_ALBUMS)]
        self.artists = [
            {
                "Id": f"ar{i}",
                "Name": n,
                "Type": "MusicArtist",
                "ChildCount": sum(1 for a in self.albums if a["AlbumArtist"] == n),
            }
            for i, n in enumerate(ARTISTS)
        ]
        self.playlists = [
            {"Id": f"pl{i}", "Name": f"Playlist {i}", "Type": "Playlist", "ChildCount": 5}
            for i in range(4)
        ]
        self.songs = sorted(
            (_track(a, n) for a in self.albums[:30] for n in range(1, 4)), key=lambda t: t["Name"]
        )
        self.calls: List[tuple] = []

    def get_items(
        self,
        parent_id,
        item_types,
        limit=100,
        start=0,
        sort_by="SortName",
        sort_order="Ascending",
        recursive=True,
        genre_ids="",
        filters="",
        **kw,
    ):
        self.calls.append(("get_items", item_types, start, limit))
        if item_types == "MusicAlbum":
            pool = [a for a in self.albums if not genre_ids or a["Genre"] == genre_ids]
        elif item_types == "Playlist":
            pool = self.playlists
        elif item_types == "Audio":
            pool = self.songs
        else:
            pool = []
        if "IsFavorite" in (filters or ""):
            pool = [x for x in pool if (x.get("UserData") or {}).get("IsFavorite")]
        page = pool[start : start + limit]
        return {"Items": [dict(a) for a in page], "TotalRecordCount": len(pool)}

    def get_item(self, item_id):
        self.calls.append(("get_item", item_id))
        for pool in (self.albums, self.artists, self.playlists):
            for x in pool:
                if x["Id"] == item_id:
                    return dict(x)
        return {}

    def get_album_tracks(self, album_id):
        self.calls.append(("get_album_tracks", album_id))
        album = next(a for a in self.albums if a["Id"] == album_id)
        return [_track(album, n) for n in range(1, TRACKS_PER_ALBUM + 1)]

    def get_artists(self, limit=200, start_index=0, parent_id=""):
        self.calls.append(("get_artists", start_index, limit))
        return [dict(a) for a in self.artists[start_index : start_index + limit]]

    def get_artist_albums(self, artist_id):
        self.calls.append(("get_artist_albums", artist_id))
        return [dict(a) for a in self.albums if a["AlbumArtists"][0]["Id"] == artist_id]

    def get_genres(self):
        return [{"Id": g, "Name": g, "Type": "MusicGenre"} for g in GENRES]

    def get_playlist_items(self, playlist_id):
        self.calls.append(("get_playlist_items", playlist_id))
        n = int(playlist_id[2:])
        return [_track(self.albums[(n * 7 + k) % len(self.albums)], 1 + k % 3) for k in range(5)]

    def search_all(self, term, songs=12, albums=14, artists=14):
        self.calls.append(("search_all", term))
        t = term.lower()
        return {
            "MusicArtist": [a for a in self.artists if t in a["Name"].lower()][:artists],
            "MusicAlbum": [a for a in self.albums if t in a["Name"].lower()][:albums],
            "Audio": [s for s in self.songs if t in s["Name"].lower()][:songs],
        }

    def get_instant_mix(self, item_id, count=50):
        self.calls.append(("get_instant_mix", item_id))
        return self.songs[:10]

    def get_similar_songs(self, item_id, count=50):
        self.calls.append(("get_similar_songs", item_id))
        return self.songs[:10]

    def get_genre_radio(self, genre, count=50):
        self.calls.append(("get_genre_radio", genre))
        return self.songs[:10]

    def get_lyrics(self, item_id):
        self.calls.append(("get_lyrics", item_id))
        return {
            "Lyrics": [{"Text": f"Line {i}", "Start": i * 5_000 * 10_000} for i in range(1, 30)]
        }

    def probe(self, server_url):
        self.calls.append(("probe", server_url))
        if "nothing-here" in server_url:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(product_name="Fakefin")

    def authenticate(self, server_url, username, password):
        self.calls.append(("authenticate", server_url, username))
        if password != self.password:
            raise RuntimeError("401 Unauthorized")
        self.is_authenticated = True

    def server_logout(self):
        self.calls.append(("server_logout",))
        self.is_authenticated = False
        return True

    def get_image_url(self, item_id, kind="Primary", size=0, *a, **k):
        return ""  # no network: covers fall back to the surface colour

    def toggle_favorite(self, item_id, favorite):
        self.calls.append(("toggle_favorite", item_id, favorite))


@pytest.fixture
def fake_provider(monkeypatch):
    import jellytoast.providers as providers
    from jellytoast.quick import models

    prov = FakeProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: prov)
    monkeypatch.setattr(models, "_provider", lambda: prov)
    return prov


@pytest.fixture
def quick(qapp, fake_provider):
    """The real Main.qml over the fake library, at phone size, with a Rig."""
    from jellytoast import quick as quick_ui
    from jellytoast.player_state import NowPlaying, get_now_playing, set_now_playing
    from jellytoast.quick.rig import Rig
    from jellytoast.settings import get_settings

    settings = get_settings()
    saved_section = settings.quick_section
    settings.quick_section = "album"  # every test starts on Albums
    saved_np = get_now_playing()
    set_now_playing(NowPlaying())  # global — never inherit another test's track
    engine = quick_ui.build_engine(qapp, start_core=False)
    rig = Rig(engine)
    rig.provider = fake_provider
    rig.window.resize(402, 874)
    if fake_provider.is_authenticated:
        engine._jt_keep[1]["albums"].reload()
        rig.wait_until(
            lambda: rig.engine.rootContext().contextProperty("albums").property("count") > 0
        )
    rig.wait_idle()
    yield rig
    # Really destroy the window + engine: a closed-but-alive window can keep
    # the (global) mouse device's grab and swallow the next test's taps.
    from PySide6.QtCore import QCoreApplication, QEvent

    rig.window.close()
    rig.window.deleteLater()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    set_now_playing(saved_np)
    settings.quick_section = saved_section
