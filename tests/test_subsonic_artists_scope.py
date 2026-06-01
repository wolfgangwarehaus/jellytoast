"""SubsonicProvider artists-surface library scoping (multi-library picker).

Regression for the parity bug where ``get_items(MusicArtist)`` dropped the
selected library: Albums + Songs forwarded ``musicFolderId`` to the server
but Artists did not, so picking one library left the Artists tab showing
every folder's artists (while it scoped correctly on Jellyfin). Pure logic
— stubs ``_request``, no network / Qt.
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
    def __init__(self, response):
        self.calls = []
        self.response = response

    def __call__(self, path, params=None, server_url=None):
        self.calls.append((path, params or {}))
        return self.response


_ARTISTS = {
    "artists": {
        "index": [
            {"name": "A", "artist": [{"id": "ar1", "name": "Alpha"}]},
            {"name": "B", "artist": [{"id": "ar2", "name": "Beta"}]},
        ]
    }
}


class TestGetArtistsScope:
    def test_get_artists_forwards_music_folder(self, monkeypatch):
        rec = _Recorder(_ARTISTS)
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p.get_artists(parent_id="disc")

        path, params = rec.calls[0]
        assert path == "getArtists"
        assert params["musicFolderId"] == "disc"

    def test_get_artists_omits_folder_when_unscoped(self, monkeypatch):
        rec = _Recorder(_ARTISTS)
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p.get_artists()  # 'all' / nothing selected

        _, params = rec.calls[0]
        assert "musicFolderId" not in params

    def test_get_items_artist_branch_scopes_by_parent(self, monkeypatch):
        # The real entry point the grid uses: get_items(MusicArtist) must
        # thread parent_id → getArtists, matching the MusicAlbum / Audio
        # branches (the cross-provider parity break this fixes).
        rec = _Recorder(_ARTISTS)
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        out = p.get_items("disc", "MusicArtist")

        path, params = rec.calls[0]
        assert path == "getArtists"
        assert params["musicFolderId"] == "disc"
        assert [it["Id"] for it in out["Items"]] == ["ar1", "ar2"]

    def test_get_items_artist_branch_unscoped_when_all(self, monkeypatch):
        rec = _Recorder(_ARTISTS)
        p = _provider()
        monkeypatch.setattr(p, "_request", rec)

        p.get_items("", "MusicArtist")  # 'all' → empty parent

        _, params = rec.calls[0]
        assert "musicFolderId" not in params
