"""Tests for the stale-node helpers in ``offline.index``.

A blob goes ``stale`` when a re-sync detects the server-side audio
file changed — the user still has a local copy, but it's mismatched
with the server. ``mark_stale`` only flips a ``complete`` node; the
other states are deliberately untouched (a ``pending`` row half-way
through a download has no stable blob to be stale against).
"""

from __future__ import annotations

from jellytoast.offline import index as _index


def _add(item_id, kind="track", *, state="pending", name=None):
    meta = {"Name": name or item_id}
    return _index.upsert_node(item_id, kind, meta, False, state=state)


class TestMarkStale:
    def test_flips_complete_to_stale(self, offline_db):
        _add("t1", state="complete")
        _index.mark_stale("t1")
        assert _index.get_node("t1")["state"] == "stale"

    def test_leaves_pending_alone(self, offline_db):
        _add("t1", state="pending")
        _index.mark_stale("t1")
        assert _index.get_node("t1")["state"] == "pending"

    def test_leaves_downloading_alone(self, offline_db):
        _add("t1", state="downloading")
        _index.mark_stale("t1")
        assert _index.get_node("t1")["state"] == "downloading"

    def test_leaves_failed_alone(self, offline_db):
        _add("t1", state="failed")
        _index.mark_stale("t1")
        assert _index.get_node("t1")["state"] == "failed"

    def test_already_stale_stays_stale(self, offline_db):
        _add("t1", state="complete")
        _index.mark_stale("t1")
        _index.mark_stale("t1")  # second call is a no-op
        assert _index.get_node("t1")["state"] == "stale"

    def test_missing_node_is_noop(self, offline_db):
        # No exception when the id doesn't exist.
        _index.mark_stale("ghost")
        assert _index.get_node("ghost") is None


class TestListStaleItems:
    def test_returns_empty_when_no_stale(self, offline_db):
        _add("t1", state="complete")
        assert _index.list_stale_items() == []

    def test_returns_stale_nodes(self, offline_db):
        _add("t1", state="complete")
        _add("t2", state="complete")
        _index.mark_stale("t1")
        out = _index.list_stale_items()
        ids = [r["item_id"] for r in out]
        assert ids == ["t1"]

    def test_filters_by_kind(self, offline_db):
        _add("t1", "track", state="complete")
        _add("al1", "album", state="complete")
        _index.mark_stale("t1")
        _index.mark_stale("al1")
        track_only = [r["item_id"] for r in _index.list_stale_items(kind="track")]
        assert track_only == ["t1"]
        album_only = [r["item_id"] for r in _index.list_stale_items(kind="album")]
        assert album_only == ["al1"]

    def test_lifts_name_from_metadata(self, offline_db):
        _add("t1", state="complete", name="Old Name")
        _index.mark_stale("t1")
        rows = _index.list_stale_items()
        assert rows[0]["name"] == "Old Name"
        assert rows[0]["metadata"]["Name"] == "Old Name"

    def test_recompute_state_allows_stale_terminal(self, offline_db):
        # _TERMINAL_STATES already includes 'stale' — a parent whose
        # children are mix of complete + stale should still roll up
        # cleanly (failed, by the existing rule: terminal but not all
        # complete).
        _add("album1", "album", state="downloading")
        _add("t1", state="complete")
        _add("t2", state="complete")
        _index.link("album1", "t1")
        _index.link("album1", "t2")
        _index.mark_stale("t1")
        assert _index.recompute_state("album1") == "failed"
