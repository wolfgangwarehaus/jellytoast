"""Tests for the offline download manager (``modules/offline/manager.py``).

The manager's threading / dispatch / progress-emission machinery needs
a live worker pool and bus to exercise meaningfully — out of scope for
a unit test. What *is* unit-testable is the worker-side planning:

- ``_ext_for`` — Content-Type / container -> file extension.
- ``_plan`` — recursive expansion of an item into the node graph,
  returning the flat list of leaf tracks to download. This is the
  bridge between snapshot.freeze and the index graph, and the place
  the "root is requested, children are not" rule lives.
"""

from __future__ import annotations

import pytest

from modules.offline import index as _index
from modules.offline import manager as _mgr


# ── _ext_for ────────────────────────────────────────────────────────────────


class TestExtFor:
    @pytest.mark.parametrize("content_type,expected", [
        ("audio/flac", "flac"),
        ("audio/x-flac", "flac"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("audio/ogg", "ogg"),
        ("audio/flac; charset=binary", "flac"),   # params stripped
        ("AUDIO/FLAC", "flac"),                   # case-insensitive
    ])
    def test_known_content_types(self, content_type, expected):
        assert _mgr._ext_for(content_type, "") == expected

    def test_falls_back_to_container_hint(self):
        assert _mgr._ext_for("application/octet-stream", "FLAC") == "flac"
        assert _mgr._ext_for("", ".m4a") == "m4a"     # leading dot stripped

    def test_neutral_default_when_nothing_known(self):
        assert _mgr._ext_for("", "") == "audio"
        assert _mgr._ext_for(None, None) == "audio"


# ── _plan ───────────────────────────────────────────────────────────────────


class _FakeProvider:
    """Canned child fetches for snapshot.freeze, called by _plan."""

    def get_album_tracks(self, item_id):
        return [
            {"Id": "t1", "Type": "Audio", "Name": "One"},
            {"Id": "t2", "Type": "Audio", "Name": "Two"},
        ]

    def get_artist_albums(self, item_id):
        return [{"Id": "al1", "Type": "MusicAlbum", "Name": "Album"}]

    def get_playlist_items(self, item_id):
        # A track repeated — _plan links it, dedup happens in _ingest.
        return [
            {"Id": "t1", "Type": "Audio", "Name": "One"},
            {"Id": "t1", "Type": "Audio", "Name": "One"},
        ]


@pytest.fixture
def fake_provider(monkeypatch):
    import modules.providers as providers_mod
    monkeypatch.setattr(providers_mod, "_PROVIDER", _FakeProvider())


class TestPlan:
    def test_track_plans_to_itself(self, offline_db, fake_provider):
        item = {"Id": "t1", "Type": "Audio", "Name": "One"}
        leaves = _mgr._plan(item, requested=True)
        assert [leaf["Id"] for leaf in leaves] == ["t1"]
        node = _index.get_node("t1")
        assert node is not None
        assert node["kind"] == "track"
        assert node["requested"] == 1

    def test_album_expands_and_links_tracks(self, offline_db, fake_provider):
        item = {"Id": "al1", "Type": "MusicAlbum", "Name": "Album"}
        leaves = _mgr._plan(item, requested=True)
        # Leaves are the album's tracks.
        assert sorted(leaf["Id"] for leaf in leaves) == ["t1", "t2"]
        # Album node exists and is the requested root; tracks are linked
        # children, NOT requested.
        assert _index.get_node("al1")["requested"] == 1
        assert sorted(_index.children("al1")) == ["t1", "t2"]
        assert _index.get_node("t1")["requested"] == 0
        assert _index.get_node("t2")["requested"] == 0

    def test_artist_expands_recursively(self, offline_db, fake_provider):
        item = {"Id": "ar1", "Type": "MusicArtist", "Name": "Artist"}
        leaves = _mgr._plan(item, requested=True)
        # artist -> album -> tracks, flattened to the leaf tracks.
        assert sorted(leaf["Id"] for leaf in leaves) == ["t1", "t2"]
        # Full graph linked: artist -> album -> each track.
        assert _index.children("ar1") == ["al1"]
        assert sorted(_index.children("al1")) == ["t1", "t2"]
        assert _index.parents("t1") == ["al1"]
        # Only the artist is the user-requested root.
        assert _index.get_node("ar1")["requested"] == 1
        assert _index.get_node("al1")["requested"] == 0
        assert _index.get_node("t1")["requested"] == 0

    def test_repeated_child_links_once(self, offline_db, fake_provider):
        # A playlist listing the same track twice — link is idempotent,
        # so the graph has one edge (dedup of the *job* is _ingest's).
        item = {"Id": "pl1", "Type": "Playlist", "Name": "Mix"}
        leaves = _mgr._plan(item, requested=True)
        assert [leaf["Id"] for leaf in leaves] == ["t1", "t1"]
        assert _index.children("pl1") == ["t1"]      # one edge, not two
