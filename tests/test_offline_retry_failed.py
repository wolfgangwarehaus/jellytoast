"""Tests for ``manager.retry_failed`` — re-queue every failed node.

A failed track-leaf gets put back on the queue; a failed cascade root
(album / artist / playlist) flips back to ``pending`` so its state can
roll up again as its children finish, but doesn't earn its own blob
job (it never had one). Untouched nodes (complete, downloading,
pending) stay where they are.
"""

from __future__ import annotations

import pytest

from jellytoast.offline import index as _index
from jellytoast.offline import manager as _mgr


@pytest.fixture(autouse=True)
def _reset_manager():
    _mgr._reset_for_tests()
    yield
    _mgr._reset_for_tests()


@pytest.fixture
def fake_settings(monkeypatch):
    class _FakeSettings:
        def __init__(self):
            self.downloads_paused = False

    fake = _FakeSettings()
    import jellytoast.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def bus_spy(monkeypatch):
    events = []

    class _Signal:
        def __init__(self, name):
            self._name = name

        def emit(self, *args):
            events.append((self._name, args))

        def connect(self, *a, **k):
            pass

    class _Bus:
        download_queue_paused = _Signal("paused")
        download_queue_resumed = _Signal("resumed")
        download_progress = _Signal("progress")

    bus = _Bus()
    import jellytoast.player_state as ps

    monkeypatch.setattr(ps.PlayerBus, "get", classmethod(lambda cls: bus))
    return events


@pytest.fixture
def no_dispatch(monkeypatch):
    """Stop _dispatch from actually starting downloads or even popping
    the queue — retry_failed re-queues items, and we want to observe
    those queue + jobs structures before the dispatcher drains them."""
    monkeypatch.setattr(_mgr, "_dispatch", lambda: None)


def _add(item_id, kind, state, metadata=None):
    meta = metadata or {"Id": item_id, "Name": item_id}
    _index.upsert_node(item_id, kind, meta, requested=False, state=state)


class TestRetryFailed:
    def test_returns_zero_when_no_failed_nodes(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t1", "track", "complete")
        assert _mgr.retry_failed() == 0

    def test_moves_failed_tracks_to_pending(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t1", "track", "failed")
        _add("t2", "track", "failed")
        count = _mgr.retry_failed()
        assert count == 2
        assert _index.get_node("t1")["state"] == "pending"
        assert _index.get_node("t2")["state"] == "pending"

    def test_re_enqueues_failed_tracks(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t1", "track", "failed")
        _mgr.retry_failed()
        assert "t1" in _mgr._queue
        assert "t1" in _mgr._jobs

    def test_leaves_non_failed_nodes_untouched(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t_done", "track", "complete")
        _add("t_run", "track", "downloading")
        _add("t_wait", "track", "pending")
        _add("t_dead", "track", "failed")
        _mgr.retry_failed()
        assert _index.get_node("t_done")["state"] == "complete"
        assert _index.get_node("t_run")["state"] == "downloading"
        assert _index.get_node("t_wait")["state"] == "pending"
        assert _index.get_node("t_dead")["state"] == "pending"

    def test_cascade_root_flips_back_but_no_blob_job(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        # An album whose child failed: album shows up as failed too via
        # roll-up. retry_failed flips both back, but only the leaf
        # track gets a queued job — the album has no blob of its own.
        _add("album1", "album", "failed")
        _add("t1", "track", "failed")
        _index.link("album1", "t1")
        count = _mgr.retry_failed()
        assert count == 1  # only the track leaf
        assert _index.get_node("album1")["state"] == "pending"
        assert _index.get_node("t1")["state"] == "pending"
        assert "t1" in _mgr._queue
        assert "album1" not in _mgr._queue

    def test_emits_pending_progress_per_retry(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t1", "track", "failed")
        _mgr.retry_failed()
        progress = [e for e in bus_spy if e[0] == "progress"]
        # One emit for the retried track, state="pending".
        assert ("progress", ("t1", "pending", 0.0)) in progress

    def test_skips_re_enqueue_if_already_queued(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        _add("t1", "track", "failed")
        # Pre-populate so retry doesn't double-add (e.g. user spam-
        # clicked).
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        count = _mgr.retry_failed()
        # State still flips, but the queue doesn't double-up.
        assert _index.get_node("t1")["state"] == "pending"
        assert list(_mgr._queue).count("t1") == 1
        assert count == 0

    def test_uses_frozen_metadata_for_job_item(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
    ):
        # The job needs an item dict for _start_download — pull it from
        # the snapshot rather than reconstructing on the fly.
        meta = {"Id": "t1", "Name": "Resurrected", "Container": "flac"}
        _add("t1", "track", "failed", metadata=meta)
        _mgr.retry_failed()
        assert _mgr._jobs["t1"]["item"]["Name"] == "Resurrected"

    def test_underscore_url_does_not_retry_other_server(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        monkeypatch,
    ):
        # #69: retry_failed is identity-scoped via an ESCAPEd LIKE, so a
        # server URL containing '_' can't wildcard-match another co-resident
        # server's failed nodes in a shared downloads.db ('media_server' vs
        # 'mediaXserver' differ only at the '_'/'X').
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://mediaXserver"
        )
        _add("t-other", "track", "failed")
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://media_server"
        )
        _add("t-own", "track", "failed")

        count = _mgr.retry_failed()

        # Only our server's failed track is retried.
        assert count == 1
        assert "t-own" in _mgr._queue
        assert "t-other" not in _mgr._queue
        # The other server's failed row stays 'failed' — not flipped to
        # 'pending' nor re-enqueued.
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://mediaXserver"
        )
        assert _index.get_node("t-other")["state"] == "failed"
