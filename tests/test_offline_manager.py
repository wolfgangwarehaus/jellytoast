"""Tests for the offline download manager (``jellytoast/offline/manager.py``).

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

from jellytoast.offline import index as _index
from jellytoast.offline import manager as _mgr

# ── _ext_for ────────────────────────────────────────────────────────────────


class TestExtFor:
    @pytest.mark.parametrize(
        "content_type,expected",
        [
            ("audio/flac", "flac"),
            ("audio/x-flac", "flac"),
            ("audio/mpeg", "mp3"),
            ("audio/mp4", "m4a"),
            ("audio/ogg", "ogg"),
            ("audio/flac; charset=binary", "flac"),  # params stripped
            ("AUDIO/FLAC", "flac"),  # case-insensitive
        ],
    )
    def test_known_content_types(self, content_type, expected):
        assert _mgr._ext_for(content_type, "") == expected

    def test_falls_back_to_container_hint(self):
        assert _mgr._ext_for("application/octet-stream", "FLAC") == "flac"
        assert _mgr._ext_for("", ".m4a") == "m4a"  # leading dot stripped

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
    import jellytoast.providers as providers_mod

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
        assert _index.children("pl1") == ["t1"]  # one edge, not two


class TestRemoveSharedInFlight:
    """#7: cancelling one parent of an in-flight track shared with ANOTHER
    live parent must NOT cancel the track — otherwise _finish discards the
    fragment and skips the cascade bump, wedging the surviving parent's
    counter at 'downloading' forever."""

    def _reset_mgr(self):
        _mgr._active.clear()
        _mgr._cancelled.clear()
        _mgr._jobs.clear()
        _mgr._pending.clear()

    def test_shared_in_flight_track_survives_one_parents_removal(self, offline_db):
        # P -> T <- A : track T shared by playlist P and album A.
        for nid, kind in (("P", "playlist"), ("A", "album"), ("T", "track")):
            _index.upsert_node(nid, kind, {"Name": nid}, True, state="downloading")
        _index.link("P", "T")
        _index.link("A", "T")
        self._reset_mgr()
        _mgr._active.add("T")
        _mgr._jobs["T"] = {"parents": {"P", "A"}}
        try:
            _mgr.remove("P")
            # T still held by A → not cancelled, job intact, A edge survives.
            assert "T" not in _mgr._cancelled
            assert "T" in _mgr._jobs
            assert "A" in _index.parents("T")
        finally:
            self._reset_mgr()

    def test_orphaned_in_flight_track_is_cancelled(self, offline_db):
        # P -> T only: removing P orphans T, which must be cancelled.
        for nid, kind in (("P", "playlist"), ("T", "track")):
            _index.upsert_node(nid, kind, {"Name": nid}, True, state="downloading")
        _index.link("P", "T")
        self._reset_mgr()
        _mgr._active.add("T")
        _mgr._jobs["T"] = {"parents": {"P"}}
        try:
            _mgr.remove("P")
            assert "T" in _mgr._cancelled
            assert "T" not in _mgr._jobs
        finally:
            self._reset_mgr()


class TestIngestPlanCancelledInFlightRace:
    """#403: re-requesting a track whose original GET was cancelled by a
    remove() (still in _active + _cancelled, _jobs popped) must NOT mint a
    fresh queued job — _dispatch would pop it, see the stale _cancelled
    flag, silently drop the re-request AND eat the flag meant for the
    in-flight GET (which then commits the removed track + discards the
    re-attached parents). _ingest_plan voids the stale cancel and fulfils
    the re-request via the still-unwinding in-flight GET."""

    def _reset_mgr(self):
        _mgr._active.clear()
        _mgr._cancelled.clear()
        _mgr._jobs.clear()
        _mgr._pending.clear()
        _mgr._queue.clear()

    def test_track_rerequest_voids_stale_cancel_and_reuses_inflight(
        self, offline_db, monkeypatch
    ):
        monkeypatch.setattr(_mgr, "_dispatch", lambda: None)
        _index.upsert_node("T", "track", {"Name": "T"}, True, state="downloading")
        self._reset_mgr()
        # Post-remove-mid-flight: GET still unwinding, cancel flagged, job popped.
        _mgr._active.add("T")
        _mgr._cancelled.add("T")
        try:
            _mgr._ingest_plan("T", "track", [{"Id": "T", "Type": "Audio", "Name": "T"}])
            assert "T" not in _mgr._cancelled  # stale cancellation voided
            assert "T" in _mgr._jobs  # job restored so the in-flight _finish lands
            assert "T" not in _mgr._queue  # NOT re-queued — in-flight GET fulfils it
        finally:
            self._reset_mgr()

    def test_cascade_rerequest_reattaches_parent_without_requeue(
        self, offline_db, monkeypatch
    ):
        monkeypatch.setattr(_mgr, "_dispatch", lambda: None)
        _index.upsert_node("T", "track", {"Name": "T"}, True, state="downloading")
        self._reset_mgr()
        _mgr._active.add("T")
        _mgr._cancelled.add("T")
        try:
            _mgr._ingest_plan("A", "album", [{"Id": "T", "Type": "Audio", "Name": "T"}])
            # The new root is re-attached so its cascade counter rolls up
            # when the in-flight GET commits (vs wedging at "downloading").
            assert _mgr._jobs["T"]["parents"] == {"A"}
            assert "T" not in _mgr._queue
            assert "T" not in _mgr._cancelled
            assert _mgr._pending["A"] == {"total": 1, "remaining": 1}
        finally:
            self._reset_mgr()

    def test_active_not_cancelled_attaches_without_requeue(
        self, offline_db, monkeypatch
    ):
        # Normal re-request of an actively-downloading track (no remove):
        # attach the new root, don't double-queue. Guards the fix against
        # over-reaching on the common case.
        monkeypatch.setattr(_mgr, "_dispatch", lambda: None)
        _index.upsert_node("T", "track", {"Name": "T"}, True, state="downloading")
        self._reset_mgr()
        _mgr._active.add("T")
        _mgr._jobs["T"] = {
            "item": {"Id": "T"},
            "parents": {"P"},
            "total_bytes": 0,
            "got_bytes": 0,
        }
        try:
            _mgr._ingest_plan("A", "album", [{"Id": "T", "Type": "Audio", "Name": "T"}])
            assert _mgr._jobs["T"]["parents"] == {"P", "A"}
            assert "T" not in _mgr._queue
        finally:
            self._reset_mgr()


# ── reset_queue (server-identity change drain) ──────────────────────────────


class TestResetQueue:
    """reset_queue() drains in-flight + queued downloads on a server-identity
    change (server swap / sign-out) so a job planned under the old server
    can't keep committing into the new (or signed-out) offline library."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        _mgr._reset_for_tests()
        yield
        _mgr._reset_for_tests()

    def test_drops_queued_cancels_in_flight_and_resets_counters(self, qapp):
        # One in-flight (active, dispatched) job + one queued-but-unstarted.
        _mgr._active.add("active1")
        _mgr._jobs["active1"] = {"item": {"Id": "active1"}, "parents": set()}
        _mgr._pending["active1"] = {"got": 0}
        _mgr._queue.append("queued1")
        _mgr._session_total = 5
        _mgr._session_expected_total = 5

        _mgr.reset_queue()

        # Queued work dropped; per-job state cleared.
        assert len(_mgr._queue) == 0
        assert _mgr._jobs == {}
        assert _mgr._pending == {}
        # In-flight job flagged cancelled so its _finish discards the partial
        # rather than committing it under whoever signs in next.
        assert "active1" in _mgr._cancelled
        # Aggregate "downloading X of Y" counters cleared.
        assert _mgr._session_total == 0
        assert _mgr._session_expected_total == 0
