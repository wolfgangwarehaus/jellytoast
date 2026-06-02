"""Tests for the offline node-graph index (``modules/offline/index.py``).

The index owns node identity and the parent/child graph that makes "a
track in two playlists is one blob with two edges" a property of the
system. These tests exercise the graph mutations and the cascade-delete
orphan-reaping that Phases 2–3 landed but never had coverage for.

Isolation: the shared ``offline_db`` fixture (conftest.py) redirects
the blob/DB location to a tmp dir and resets ``db._conn`` so each test
gets a fresh, empty ``downloads.db`` — never the real one.
"""

from __future__ import annotations

from modules.offline import index as _index


def _add(item_id, kind="track", *, requested=False, state="pending", name=None):
    meta = {"Name": name or item_id}
    return _index.upsert_node(item_id, kind, meta, requested, state=state)


# ── identity ────────────────────────────────────────────────────────────────


class TestNodeIdentity:
    def test_node_id_round_trips_through_strip(self, offline_db):
        pk = _index.node_id("abc")
        assert pk.endswith(":abc")
        assert _index._strip_identity(pk) == "abc"

    def test_strip_identity_passes_through_unprefixed(self, offline_db):
        # A pk that isn't under the current identity is returned as-is.
        assert _index._strip_identity("other|host:xyz") == "other|host:xyz"


# ── upsert_node ─────────────────────────────────────────────────────────────


class TestUpsertNode:
    def test_insert_creates_node(self, offline_db):
        _add("t1", "track", state="pending")
        node = _index.get_node("t1")
        assert node is not None
        assert node["item_id"] == "t1"
        assert node["kind"] == "track"
        assert node["state"] == "pending"
        assert node["requested"] == 0

    def test_update_preserves_state(self, offline_db):
        _add("t1", state="complete")
        # Re-touching the node (e.g. an album re-walk) must NOT demote
        # state back to the default "pending".
        _index.upsert_node("t1", "track", {"Name": "fresh"}, False, state="pending")
        assert _index.get_node("t1")["state"] == "complete"

    def test_requested_escalates_but_never_clears(self, offline_db):
        _add("t1", requested=False)
        _index.upsert_node("t1", "track", {}, True)  # 0 -> 1
        assert _index.get_node("t1")["requested"] == 1
        _index.upsert_node("t1", "track", {}, False)  # 1 -> stays 1
        assert _index.get_node("t1")["requested"] == 1

    def test_update_refreshes_metadata(self, offline_db):
        _add("t1", name="old")
        _index.upsert_node("t1", "track", {"Name": "new"}, False)
        assert _index.get_node("t1")["metadata_json"] == '{"Name": "new"}'


# ── link / children / parents ───────────────────────────────────────────────


class TestEdges:
    def test_link_creates_traversable_edge(self, offline_db):
        _add("album1", "album")
        _add("t1", "track")
        _index.link("album1", "t1")
        assert _index.children("album1") == ["t1"]
        assert _index.parents("t1") == ["album1"]

    def test_link_is_idempotent(self, offline_db):
        _add("album1", "album")
        _add("t1", "track")
        _index.link("album1", "t1")
        _index.link("album1", "t1")
        assert _index.children("album1") == ["t1"]

    def test_shared_child_has_two_parents(self, offline_db):
        _add("p1", "playlist")
        _add("p2", "playlist")
        _add("t1", "track")
        _index.link("p1", "t1")
        _index.link("p2", "t1")
        assert sorted(_index.parents("t1")) == ["p1", "p2"]
        assert _index.refcount(_index.node_id("t1")) == 2


# ── state queries + roll-up ─────────────────────────────────────────────────


class TestState:
    def test_is_complete_tracks_state(self, offline_db):
        _add("t1", state="downloading")
        assert _index.is_complete("t1") is False
        _index.set_state("t1", "complete")
        assert _index.is_complete("t1") is True

    def test_complete_item_ids_returns_only_complete_nodes(self, offline_db):
        _add("t_done_a", state="complete")
        _add("t_done_b", state="complete")
        _add("t_pending", state="pending")
        _add("t_failed", state="failed")
        assert _index.complete_item_ids() == {"t_done_a", "t_done_b"}

    def test_complete_item_ids_empty_when_nothing_downloaded(self, offline_db):
        _add("t1", state="pending")
        assert _index.complete_item_ids() == set()

    def test_complete_item_ids_flips_on_state_change(self, offline_db):
        _add("t1", state="downloading")
        assert _index.complete_item_ids() == set()
        _index.set_state("t1", "complete")
        assert _index.complete_item_ids() == {"t1"}
        # cascade_delete + tomb to absent → drop from the set.
        _index.cascade_delete("t1")
        assert _index.complete_item_ids() == set()

    def test_recompute_leaf_returns_none(self, offline_db):
        # A node with no children is left untouched.
        _add("t1", state="pending")
        assert _index.recompute_state("t1") is None

    def test_recompute_all_complete_rolls_up_to_complete(self, offline_db):
        _add("album1", "album", state="downloading")
        for tid in ("t1", "t2"):
            _add(tid, state="complete")
            _index.link("album1", tid)
        assert _index.recompute_state("album1") == "complete"
        assert _index.get_node("album1")["state"] == "complete"

    def test_recompute_with_pending_child_is_downloading(self, offline_db):
        _add("album1", "album", state="downloading")
        _add("t1", state="complete")
        _add("t2", state="pending")  # not terminal
        _index.link("album1", "t1")
        _index.link("album1", "t2")
        assert _index.recompute_state("album1") == "downloading"

    def test_recompute_all_terminal_some_failed_is_failed(self, offline_db):
        _add("album1", "album", state="downloading")
        _add("t1", state="complete")
        _add("t2", state="failed")  # terminal, not complete
        _index.link("album1", "t1")
        _index.link("album1", "t2")
        assert _index.recompute_state("album1") == "failed"


# ── cascade_delete ──────────────────────────────────────────────────────────


class TestCascadeDelete:
    def test_delete_leaf_removes_only_it(self, offline_db):
        _add("t1")
        assert _index.cascade_delete("t1") == []
        assert _index.get_node("t1") is None

    def test_delete_album_reaps_orphaned_tracks(self, offline_db):
        _add("album1", "album")
        for tid in ("t1", "t2"):
            _add(tid)
            _index.link("album1", tid)
        _index.cascade_delete("album1")
        # Album gone, and its now-parentless tracks reaped with it.
        assert _index.get_node("album1") is None
        assert _index.get_node("t1") is None
        assert _index.get_node("t2") is None

    def test_shared_track_survives_one_parents_deletion(self, offline_db):
        # t1 is in two playlists — deleting one must leave t1 alone.
        _add("p1", "playlist")
        _add("p2", "playlist")
        _add("t1")
        _index.link("p1", "t1")
        _index.link("p2", "t1")

        _index.cascade_delete("p1")
        assert _index.get_node("p1") is None
        assert _index.get_node("t1") is not None  # still held by p2
        assert _index.parents("t1") == ["p2"]

        _index.cascade_delete("p2")
        assert _index.get_node("p2") is None
        assert _index.get_node("t1") is None  # now an orphan


# ── list_requested / mark_requested ─────────────────────────────────────────


class TestRequested:
    def test_list_requested_only_returns_requested_nodes(self, offline_db):
        _add("album1", "album", requested=True)
        _add("t1", "track", requested=False)  # cascade child
        ids = [r["item_id"] for r in _index.list_requested()]
        assert ids == ["album1"]

    def test_list_requested_filters_by_kind(self, offline_db):
        _add("album1", "album", requested=True)
        _add("pl1", "playlist", requested=True)
        albums = [r["item_id"] for r in _index.list_requested(kind="album")]
        assert albums == ["album1"]

    def test_list_requested_lifts_name_from_metadata(self, offline_db):
        _add("album1", "album", requested=True, name="Discovery")
        assert _index.list_requested()[0]["name"] == "Discovery"

    def test_mark_requested_escalates_existing_node(self, offline_db):
        _add("t1", "track", requested=False)
        _index.mark_requested("t1")
        assert _index.get_node("t1")["requested"] == 1
        assert [r["item_id"] for r in _index.list_requested()] == ["t1"]

    def test_mark_requested_is_noop_for_missing_node(self, offline_db):
        _index.mark_requested("ghost")  # must not raise
        assert _index.get_node("ghost") is None


class TestIdentLikeEscaping:
    """#69: identity-scoped LIKE queries escape the metacharacters in the
    server identity, so a URL with '_' (or '%') can't wildcard-match
    another server's node ids in a shared downloads.db."""

    def test_underscore_url_no_cross_server_leak(self, offline_db, monkeypatch):
        # 'media_server' and 'mediaXserver' differ only at the '_'/'X' —
        # an unescaped LIKE '...media_server...' treats '_' as a wildcard
        # and matches 'mediaXserver'.
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://media_server")
        _add("song-a", state="complete")
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://mediaXserver")
        _add("song-b", state="complete")

        # Query scoped back to media_server must not include the other
        # server's node.
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://media_server")
        ids = _index.complete_item_ids()
        assert "song-a" in ids
        assert "song-b" not in ids

    def test_list_complete_scoped_with_underscore(self, offline_db, monkeypatch):
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://a_b")
        _add("t1", kind="track", state="complete")
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://aXb")
        _add("t2", kind="track", state="complete")
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://a_b")
        items = _index.list_complete("track")
        item_ids = {it.get("item_id") for it in items}
        assert "t1" in item_ids
        assert "t2" not in item_ids

    def test_list_complete_items_public_api_scoped_with_underscore(
        self, offline_db, monkeypatch
    ):
        # The public offline.list_complete_items API (the "what's available"
        # pool the offline grids read) is identity-scoped via an ESCAPEd
        # LIKE too — so it never surfaces another co-resident server's
        # downloaded content in a shared downloads.db.
        import modules.offline as offline_pkg

        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://a_b")
        _add("t1", kind="track", state="complete")
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://aXb")
        _add("t2", kind="track", state="complete")
        monkeypatch.setattr(_index, "server_identity", lambda: "jellyfin|http://a_b")
        names = {it.get("Name") for it in offline_pkg.list_complete_items("track")}
        assert "t1" in names
        assert "t2" not in names


# ── child_snapshots (the artist page's offline fallback) ─────────────────────


class TestChildSnapshots:
    def test_returns_child_metadata(self, offline_db):
        # An artist with two downloaded child albums — the cascade-child shape
        # the offline artist page renders. db.query yields sqlite3.Row (no
        # .get()), so the body must dict() the row first; the pre-fix
        # r.get("metadata_json") raised an uncaught AttributeError here and
        # crashed the offline artist page the moment any child rows came back.
        _add("artist1", "artist", requested=True, state="complete", name="Artist One")
        _add("album1", "album", state="complete", name="Album One")
        _add("album2", "album", state="complete", name="Album Two")
        _index.link("artist1", "album1")
        _index.link("artist1", "album2")

        snaps = _index.child_snapshots("artist1")

        assert sorted(s.get("Name") for s in snaps) == ["Album One", "Album Two"]

    def test_kind_filter_excludes_other_kinds(self, offline_db):
        _add("artist1", "artist", requested=True, state="complete", name="A")
        _add("album1", "album", state="complete", name="Alb")
        _add("track1", "track", state="complete", name="Trk")
        _index.link("artist1", "album1")
        _index.link("artist1", "track1")

        albums = _index.child_snapshots("artist1", kind="album")

        assert [s.get("Name") for s in albums] == ["Alb"]

    def test_no_children_returns_empty(self, offline_db):
        _add("artist1", "artist", requested=True, state="complete", name="A")
        assert _index.child_snapshots("artist1") == []
