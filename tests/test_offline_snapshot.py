"""Tests for offline metadata snapshots (``modules/offline/snapshot.py``).

``snapshot`` interprets the provider's ``Type`` vocabulary into our
generic node ``kind`` and freezes a download's authoritative metadata:
a track is its own complete record (no round-trip), while an album /
playlist / artist does a single provider call for its direct children.
"""

from __future__ import annotations

import pytest

from modules.offline import snapshot as _snap


# ── kind_of ─────────────────────────────────────────────────────────────────


class TestKindOf:
    @pytest.mark.parametrize(
        "provider_type,expected",
        [
            ("Audio", "track"),
            ("MusicAlbum", "album"),
            ("MusicArtist", "artist"),
            ("Playlist", "playlist"),
            ("AUDIO", "track"),  # case-insensitive
        ],
    )
    def test_known_types_map(self, provider_type, expected):
        assert _snap.kind_of({"Type": provider_type}) == expected

    def test_unknown_folder_falls_back_to_album(self):
        assert _snap.kind_of({"Type": "Genre", "IsFolder": True}) == "album"

    def test_unknown_leaf_falls_back_to_track(self):
        assert _snap.kind_of({"Type": "Weird"}) == "track"

    def test_missing_type_is_track(self):
        assert _snap.kind_of({}) == "track"


# ── freeze ──────────────────────────────────────────────────────────────────


class _FakeProvider:
    """Records which child-fetch was called and returns canned lists."""

    def __init__(self):
        self.calls = []

    def get_album_tracks(self, item_id):
        self.calls.append(("album", item_id))
        return [{"Id": "t1", "Type": "Audio"}, {"Id": "t2", "Type": "Audio"}]

    def get_playlist_items(self, item_id):
        self.calls.append(("playlist", item_id))
        return [{"Id": "pt1", "Type": "Audio"}]

    def get_artist_albums(self, item_id):
        self.calls.append(("artist", item_id))
        return [{"Id": "al1", "Type": "MusicAlbum"}]


@pytest.fixture
def fake_provider(monkeypatch):
    fp = _FakeProvider()
    import modules.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    return fp


class TestFreeze:
    def test_track_is_its_own_snapshot_no_round_trip(self, fake_provider):
        item = {"Id": "t1", "Type": "Audio", "Name": "Track"}
        meta, children = _snap.freeze(item)
        assert meta == item
        assert meta is not item  # a copy, not the same dict
        assert children == []
        assert fake_provider.calls == []  # no provider call for a track

    def test_album_fetches_tracks(self, fake_provider):
        meta, children = _snap.freeze({"Id": "al1", "Type": "MusicAlbum"})
        assert fake_provider.calls == [("album", "al1")]
        assert [c["Id"] for c in children] == ["t1", "t2"]

    def test_playlist_fetches_items(self, fake_provider):
        _meta, children = _snap.freeze({"Id": "pl1", "Type": "Playlist"})
        assert fake_provider.calls == [("playlist", "pl1")]
        assert [c["Id"] for c in children] == ["pt1"]

    def test_artist_fetches_albums(self, fake_provider):
        _meta, children = _snap.freeze({"Id": "ar1", "Type": "MusicArtist"})
        assert fake_provider.calls == [("artist", "ar1")]
        assert [c["Id"] for c in children] == ["al1"]

    def test_children_are_copies(self, fake_provider):
        _meta, children = _snap.freeze({"Id": "al1", "Type": "MusicAlbum"})
        # Mutating a returned child must not bleed into later freezes.
        children[0]["Name"] = "mutated"
        _m2, children2 = _snap.freeze({"Id": "al1", "Type": "MusicAlbum"})
        assert "Name" not in children2[0]
