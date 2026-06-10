"""Tests for the offline manager's queue-level pause / resume.

Pause is "stop popping new work" — an in-flight blob isn't killed
mid-stream (a partial blob is wasted bytes; resume from a partial isn't
a Phase 6 deliverable). The flag persists across a restart through
``settings.downloads_paused`` so a user who paused yesterday doesn't
get woken up by a download tomorrow.
"""

from __future__ import annotations

import pytest

from jellytoast.offline import manager as _mgr


@pytest.fixture(autouse=True)
def _reset_manager():
    """Manager holds module-level queue + paused state — restore around
    each test so order can't leak a stuck pause flag or queued jobs."""
    _mgr._reset_for_tests()
    yield
    _mgr._reset_for_tests()


@pytest.fixture
def fake_settings(monkeypatch):
    """In-memory replacement for ``settings.downloads_paused`` — the
    real QSettings would touch the test-mode QStandardPaths file and
    leak persistence across tests."""

    class _FakeSettings:
        def __init__(self):
            self.downloads_paused = False

    fake = _FakeSettings()
    import jellytoast.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def bus_spy(monkeypatch):
    """Spy on the PlayerBus signal emissions without needing a real
    QApplication. Records ``(signal_name, args)`` in order."""
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


class TestPause:
    def test_pause_sets_flag(self, fake_settings, bus_spy):
        assert _mgr.is_paused() is False
        _mgr.pause()
        assert _mgr.is_paused() is True

    def test_pause_persists_to_settings(self, fake_settings, bus_spy):
        _mgr.pause()
        assert fake_settings.downloads_paused is True

    def test_pause_emits_signal(self, fake_settings, bus_spy):
        _mgr.pause()
        assert ("paused", ()) in bus_spy

    def test_pause_idempotent(self, fake_settings, bus_spy):
        _mgr.pause()
        _mgr.pause()
        _mgr.pause()
        paused_emits = [e for e in bus_spy if e[0] == "paused"]
        assert len(paused_emits) == 1

    def test_pause_blocks_dispatch(self, fake_settings, bus_spy):
        # Stage a job in the queue without going through enqueue (which
        # would touch the provider). With pause set, _dispatch must
        # leave it untouched.
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr.pause()
        _mgr._dispatch()
        assert "t1" in _mgr._queue
        assert "t1" not in _mgr._active


class TestResume:
    def test_resume_clears_flag(self, fake_settings, bus_spy):
        _mgr.pause()
        _mgr.resume()
        assert _mgr.is_paused() is False

    def test_resume_persists_to_settings(self, fake_settings, bus_spy):
        _mgr.pause()
        _mgr.resume()
        assert fake_settings.downloads_paused is False

    def test_resume_emits_signal(self, fake_settings, bus_spy):
        _mgr.pause()
        _mgr.resume()
        assert ("resumed", ()) in bus_spy

    def test_resume_idempotent_when_already_running(self, fake_settings, bus_spy):
        # Not paused to begin with — resume() is a no-op.
        _mgr.resume()
        _mgr.resume()
        resumed_emits = [e for e in bus_spy if e[0] == "resumed"]
        assert resumed_emits == []

    def test_resume_kicks_dispatch(self, fake_settings, bus_spy, monkeypatch):
        # Replace _start_download so resume's _dispatch doesn't try to
        # do a real provider call. Just track that it was invoked.
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr.pause()
        _mgr._dispatch()  # blocked while paused
        assert started == []

        _mgr.resume()
        assert started == ["t1"]


class TestPersistenceAcrossRestart:
    def test_paused_state_rehydrates_from_settings(self, fake_settings, bus_spy):
        # Simulate a previous session's pause baked into settings.
        fake_settings.downloads_paused = True
        # Force re-hydration: a fresh _load_paused_once call.
        _mgr._paused_loaded = False
        _mgr._paused = False
        assert _mgr.is_paused() is True
