"""Tests for offline artist-page resolution (``jellytoast/artist_page.py``).

The unit under test is the module-level ``_resolve_offline_artist``
helper extracted from ``ArtistPage._load_artist_offline``: given an
``artist_id`` plus a stubbed offline graph, it must return
``(meta, albums)`` even when the provider's id schema differs across
endpoints (the Subsonic case that prompted the bug).

We import the helper in isolation and monkeypatch ``offline.get_snapshot``
/ ``offline.child_snapshots`` / ``offline.list_complete_items`` so the
test doesn't need a real ``downloads.db``, a QApplication, or any
network.
"""

from __future__ import annotations

import pytest

from jellytoast import artist_page as _ap


@pytest.fixture
def stub_offline(monkeypatch):
    """Install stubs for the three offline accessors the resolver uses.

    Returns a ``set_state(snapshot, children, complete)`` helper that
    rewires the stubs per test. ``snapshot`` is a dict keyed by
    ``item_id``; ``children`` is a dict keyed by ``(parent_id, kind)``;
    ``complete`` is a dict keyed by ``kind``.
    """
    from jellytoast import offline as _offline

    state: dict = {"snapshot": {}, "children": {}, "complete": {}}

    def _get_snapshot(item_id):
        return state["snapshot"].get(item_id)

    def _child_snapshots(item_id, kind=None):
        return list(state["children"].get((item_id, kind), []))

    def _list_complete_items(kind=None):
        return list(state["complete"].get(kind, []))

    monkeypatch.setattr(_offline, "get_snapshot", _get_snapshot)
    monkeypatch.setattr(_offline, "child_snapshots", _child_snapshots)
    monkeypatch.setattr(_offline, "list_complete_items", _list_complete_items)

    def _set_state(*, snapshot=None, children=None, complete=None):
        state["snapshot"] = snapshot or {}
        state["children"] = children or {}
        state["complete"] = complete or {}

    return _set_state


# ── Happy path: the artist node itself is in the graph ────────────────────


class TestArtistNodePresent:
    def test_returns_snapshot_meta_and_child_albums(self, stub_offline):
        meta = {"Id": "art-1", "Name": "Air", "Type": "MusicArtist"}
        albums = [
            {"Id": "alb-1", "Name": "Moon Safari", "AlbumArtist": "Air"},
            {"Id": "alb-2", "Name": "Talkie Walkie", "AlbumArtist": "Air"},
        ]
        stub_offline(
            snapshot={"art-1": meta},
            children={("art-1", "album"): albums},
        )

        got_meta, got_albums = _ap._resolve_offline_artist("art-1")

        assert got_meta == meta
        assert got_albums == albums

    def test_artist_node_present_but_no_album_children(self, stub_offline):
        # An artist snapshot was frozen but no albums hang off it in the
        # graph. The resolver should fall through to the Id-match step;
        # if that also turns up nothing the page still gets meta + [].
        meta = {"Id": "art-1", "Name": "Air", "Type": "MusicArtist"}
        stub_offline(
            snapshot={"art-1": meta},
            complete={"album": []},
        )

        got_meta, got_albums = _ap._resolve_offline_artist("art-1")

        assert got_meta == meta
        assert got_albums == []


# ── Fallback 1: no artist node, AlbumArtists[].Id still matches ───────────


class TestAlbumArtistsIdMatch:
    def test_synthesizes_meta_and_picks_albums(self, stub_offline):
        # User downloaded one album by Air. There's no artist node in
        # the graph, but the album's AlbumArtists list does carry the
        # right artist_id, so the Id-only match wins.
        alb = {
            "Id": "alb-1",
            "Name": "Moon Safari",
            "AlbumArtist": "Air",
            "AlbumArtists": [{"Id": "art-1", "Name": "Air"}],
        }
        unrelated = {
            "Id": "alb-9",
            "Name": "Random",
            "AlbumArtist": "Someone Else",
            "AlbumArtists": [{"Id": "art-9", "Name": "Someone Else"}],
        }
        stub_offline(complete={"album": [alb, unrelated]})

        meta, albums = _ap._resolve_offline_artist("art-1")

        assert meta == {
            "Id": "art-1",
            "Name": "Air",
            "Type": "MusicArtist",
        }
        assert albums == [alb]


# ── Fallback 2: only the AlbumArtist *string* matches ─────────────────────


class TestAlbumArtistStringMatch:
    def test_subsonic_id_mismatch_resolved_by_name(self, stub_offline):
        # The bug case. The artist_id from the click context
        # ("art-track") came from a track row's ArtistItems, but the
        # album snapshot's AlbumArtists carries a *different* artist_id
        # ("art-album") for the same artist. The Id-only match misses;
        # the string-name fallback must rescue it via the
        # ArtistItems-derived name map.
        track = {
            "Id": "tr-1",
            "Name": "La Femme d'Argent",
            "AlbumId": "alb-1",
            "ArtistItems": [{"Id": "art-track", "Name": "Air"}],
        }
        alb = {
            "Id": "alb-1",
            "Name": "Moon Safari",
            "AlbumArtist": "Air",
            "AlbumArtists": [{"Id": "art-album", "Name": "Air"}],
        }
        stub_offline(
            complete={"album": [alb], "track": [track]},
        )

        meta, albums = _ap._resolve_offline_artist("art-track")

        assert meta == {
            "Id": "art-track",
            "Name": "Air",
            "Type": "MusicArtist",
        }
        assert albums == [alb]

    def test_name_match_is_case_insensitive(self, stub_offline):
        # Album snapshots can have AlbumArtist casing that differs from
        # the track's ArtistItems[].Name (e.g. "AIR" vs "Air"). The
        # match strips + lowercases so the user still sees the album.
        track = {
            "Id": "tr-1",
            "Name": "La Femme d'Argent",
            "ArtistItems": [{"Id": "art-track", "Name": "Air"}],
        }
        alb = {
            "Id": "alb-1",
            "Name": "Moon Safari",
            "AlbumArtist": " AIR ",
        }
        stub_offline(
            complete={"album": [alb], "track": [track]},
        )

        meta, albums = _ap._resolve_offline_artist("art-track")

        assert meta is not None
        assert meta["Name"] == "Air"
        assert albums == [alb]

    def test_unknown_artist_id_returns_empty(self, stub_offline):
        # No graph membership at all — neither the artist node, nor the
        # Id, nor the name. The caller still gets back ``(None, [])``
        # rather than an exception so the page can render its empty
        # state.
        stub_offline()

        meta, albums = _ap._resolve_offline_artist("never-heard-of-them")

        assert meta is None
        assert albums == []


# ── _build_artist_name_map helper sanity ──────────────────────────────────


class TestBuildArtistNameMap:
    def test_walks_tracks_and_albums(self):
        tracks = [
            {"ArtistItems": [{"Id": "a1", "Name": "Air"}]},
            {
                "ArtistItems": [
                    {"Id": "a2", "Name": "Beck"},
                    {"Id": "a3", "Name": "Charlotte Gainsbourg"},
                ]
            },
        ]
        albums = [
            {"AlbumArtists": [{"Id": "a4", "Name": "Daft Punk"}]},
            {"AlbumArtists": [{"Id": "a1", "Name": "Ignored Duplicate"}]},
        ]
        m = _ap._build_artist_name_map(tracks, albums)

        # First-write-wins so the track's "Air" beats the album's
        # collision; new ids from albums still land in the map.
        assert m == {
            "a1": "Air",
            "a2": "Beck",
            "a3": "Charlotte Gainsbourg",
            "a4": "Daft Punk",
        }

    def test_empty_inputs(self):
        assert _ap._build_artist_name_map([], []) == {}
        assert _ap._build_artist_name_map(None, None) == {}
