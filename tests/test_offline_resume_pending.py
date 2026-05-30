"""Tests for ``manager.resume_pending`` — boot-time re-queue of every
node that was mid-flight when the app last closed.

Walks the index for ``pending`` and ``downloading`` nodes, resets
``downloading`` rows back to ``pending`` (the .part fragment will be
overwritten on the next attempt), and pushes track-kind items into
``_queue``. Cascade roots roll up via their children's completion.
"""

from __future__ import annotations

import pytest

from modules.offline import index as _index
from modules.offline import manager as _mgr


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
    import modules.settings as settings_mod

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
    import modules.player_state as ps

    monkeypatch.setattr(ps.PlayerBus, "get", classmethod(lambda cls: bus))
    return events


@pytest.fixture
def no_dispatch(monkeypatch):
    monkeypatch.setattr(_mgr, "_dispatch", lambda: None)


def _add(item_id, kind, state, metadata=None):
    meta = metadata or {"Id": item_id, "Name": item_id}
    _index.upsert_node(item_id, kind, meta, requested=False, state=state)


class TestResumePending:
    def test_returns_zero_when_no_pending_nodes(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("t1", "track", "complete")
        assert _mgr.resume_pending() == 0

    def test_requeues_pending_track(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("t1", "track", "pending")
        count = _mgr.resume_pending()
        assert count == 1
        assert "t1" in _mgr._queue
        assert "t1" in _mgr._jobs

    def test_resets_downloading_to_pending(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("t1", "track", "downloading")
        _mgr.resume_pending()
        # State should now be pending again so the manager treats it
        # as fresh work.
        row = _index.get_node("t1")
        assert row["state"] == "pending"
        assert "t1" in _mgr._queue

    def test_cascade_root_stays_in_pending_but_no_blob_job(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("a1", "album", "pending")
        _add("t1", "track", "pending")
        _index.link("a1", "t1")
        count = _mgr.resume_pending()
        # Both rows count (album + track), but only the track gets a
        # blob job — cascade roots never get one.
        assert count == 1
        assert "t1" in _mgr._jobs
        assert "a1" not in _mgr._jobs

    def test_idempotent_when_already_active(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("t1", "track", "pending")
        _mgr._active.add("t1")
        # The job is already in flight (or simulated as such) — second
        # resume call must not double-queue it.
        count = _mgr.resume_pending()
        assert count == 0
        assert "t1" not in _mgr._queue

    def test_emits_pending_bus_signal_for_view_refresh(
        self, offline_db, fake_settings, bus_spy, no_dispatch
    ):
        _add("t1", "track", "pending")
        _mgr.resume_pending()
        # The bus spy collects ("progress", (item_id, state, fraction)).
        progress_events = [e for e in bus_spy if e[0] == "progress"]
        assert any(args[0] == "t1" and args[1] == "pending" for _name, args in progress_events)

    def test_underscore_url_does_not_requeue_or_mutate_other_server(
        self, offline_db, fake_settings, bus_spy, no_dispatch, monkeypatch
    ):
        # #69: resume_pending is identity-scoped via an ESCAPEd LIKE, so a
        # server URL containing '_' can't wildcard-match — and re-queue /
        # mutate — a co-resident server's nodes in a shared downloads.db.
        # 'media_server' and 'mediaXserver' differ only at the '_'/'X'; an
        # unescaped LIKE would treat '_' as a wildcard and match both.
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://mediaXserver"
        )
        _add("t-other", "track", "downloading")
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://media_server"
        )
        _add("t-own", "track", "downloading")

        count = _mgr.resume_pending()

        # Only our server's track is re-queued.
        assert count == 1
        assert "t-own" in _mgr._queue
        assert "t-own" in _mgr._jobs
        assert "t-other" not in _mgr._queue
        assert "t-other" not in _mgr._jobs
        # And the other server's row is NOT mutated — still 'downloading',
        # not flipped to 'pending'. (This is the dangerous case: a leak
        # here would re-enqueue another server's downloads.)
        monkeypatch.setattr(
            _index, "server_identity", lambda: "jellyfin|http://mediaXserver"
        )
        assert _index.get_node("t-other")["state"] == "downloading"


class TestClearAll:
    def test_removes_every_user_requested_download(
        self, offline_db, fake_settings, bus_spy, no_dispatch, monkeypatch
    ):
        import modules.offline as offline_pkg

        # Three user-requested downloads, two cascade roots + a track.
        removed: list = []
        monkeypatch.setattr(offline_pkg, "list_downloads", lambda: [
            {"item_id": "a1", "kind": "album"},
            {"item_id": "p1", "kind": "playlist"},
            {"item_id": "t1", "kind": "track"},
        ])
        monkeypatch.setattr(_mgr, "remove", lambda i: removed.append(i))

        count = offline_pkg.clear_all()
        assert count == 3
        assert removed == ["a1", "p1", "t1"]

    def test_one_failure_does_not_abort_sweep(
        self, offline_db, fake_settings, bus_spy, no_dispatch, monkeypatch
    ):
        import modules.offline as offline_pkg

        removed: list = []

        def _remove(item_id):
            if item_id == "p1":
                raise RuntimeError("simulated error")
            removed.append(item_id)

        monkeypatch.setattr(offline_pkg, "list_downloads", lambda: [
            {"item_id": "a1", "kind": "album"},
            {"item_id": "p1", "kind": "playlist"},
            {"item_id": "t1", "kind": "track"},
        ])
        monkeypatch.setattr(_mgr, "remove", _remove)

        count = offline_pkg.clear_all()
        # p1 raised so only a1 + t1 made it through; count reflects
        # successful removes, not attempted.
        assert count == 2
        assert removed == ["a1", "t1"]

    def test_empty_list_returns_zero(
        self, offline_db, fake_settings, bus_spy, no_dispatch, monkeypatch
    ):
        import modules.offline as offline_pkg

        monkeypatch.setattr(offline_pkg, "list_downloads", lambda: [])
        assert offline_pkg.clear_all() == 0
