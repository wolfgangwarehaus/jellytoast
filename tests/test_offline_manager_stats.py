"""Tests for the offline manager's aggregate queue stats — slice A of
the downloads-progress UI feature.

What slice A ships, exercised here:

- ``download_queue_stats`` bus signal carrying
  ``(active, total_session, speed_bps, eta_seconds)``.
- Per-tid byte-rate sampling over a 3-second rolling window.
- Speed = sum of per-tid rates; ETA = longest projected remaining for
  any active job with a known total + positive rate, capped at 12 h.
- Session counters (``_session_total`` / ``_session_failed``) that bump
  on dispatch / record-failure and reset on the drain edge.
- Drain-edge detection in ``_finish``: when both queue + active drain
  with at least one dispatch this cycle, ``_emit_drain_complete`` fires
  a ``notifications.notify`` call (gated on
  ``settings.notify_on_download_complete``) and emits one final
  ``download_queue_stats(0, 0, 0.0, 0.0)`` so subscribers can hide.
- ``get_queue_stats()`` one-shot snapshot for page-opened-mid-flight
  priming.
- Wi-Fi-only blocking: active jobs drain to zero, no drain edge while
  the queue still has waiting work.
"""

from __future__ import annotations

import pytest

from modules.offline import manager as _mgr


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_manager():
    """Manager holds module-level queue + session-counter state.
    Restore between tests so a stuck counter can't leak across cases."""
    _mgr._reset_for_tests()
    yield
    _mgr._reset_for_tests()


@pytest.fixture
def fake_settings(monkeypatch):
    """In-memory replacement for ``get_settings`` — the real QSettings
    would touch the test-mode QStandardPaths file and leak state."""

    class _FakeSettings:
        def __init__(self):
            self.downloads_paused = False
            self.downloads_wifi_only = False
            self.notify_on_download_complete = True

    fake = _FakeSettings()
    import modules.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def bus_spy(monkeypatch):
    """Record bus emissions in order. Shape matches the slim spies in
    ``test_offline_pause_resume`` / ``test_offline_wifi_only``."""
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
        download_queue_stats = _Signal("stats")
        downloads_wifi_only_changed = _Signal("wifi_only_changed")

    bus = _Bus()
    import modules.player_state as ps

    monkeypatch.setattr(ps.PlayerBus, "get", classmethod(lambda cls: bus))
    return events


@pytest.fixture
def notify_spy(monkeypatch):
    """Capture every ``notifications.notify`` call so the drain-edge
    tests can assert without depending on ``notify-send``."""
    calls = []

    def _capture(title, body="", icon=None, app_name="jellytoast"):
        calls.append({"title": title, "body": body})

    import modules.notifications as notifications_mod

    monkeypatch.setattr(notifications_mod, "notify", _capture)
    return calls


@pytest.fixture
def no_timer(monkeypatch):
    """Replace the QTimer construction with a no-op so the manager
    doesn't try to spin up Qt's event loop in a headless test. Tests
    drive the tick by calling ``_stats_tick`` directly."""
    monkeypatch.setattr(_mgr, "_ensure_stats_timer", lambda: None)
    monkeypatch.setattr(_mgr, "_stop_stats_timer", lambda: None)


# ── Signal shape ───────────────────────────────────────────────────────────


class TestSignalShape:
    def test_stats_tick_emits_four_int_int_float_float(
        self, fake_settings, bus_spy, no_timer
    ):
        # One in-flight job, no samples → speed = 0, eta = -1.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._active.add("t1")
        _mgr._session_total = 1

        _mgr._stats_tick()

        stats = [e for e in bus_spy if e[0] == "stats"]
        assert len(stats) == 1
        active, total_session, speed_bps, eta = stats[0][1]
        assert isinstance(active, int)
        assert isinstance(total_session, int)
        assert isinstance(speed_bps, float)
        assert isinstance(eta, float)
        assert active == 1
        assert total_session == 1
        assert speed_bps == 0.0
        assert eta == -1.0


# ── Per-job rate sampling ───────────────────────────────────────────────────


class TestRateSampling:
    def test_single_sample_yields_zero_rate(self):
        samples = [(100.0, 1024)]
        assert _mgr._compute_rate(samples) == 0.0

    def test_two_samples_known_interval(self):
        # 1 MB delivered over 1 second → 1_048_576 bytes/sec.
        samples = [(100.0, 0), (101.0, 1_048_576)]
        rate = _mgr._compute_rate(samples)
        assert abs(rate - 1_048_576) < 1e-6

    def test_three_samples_smoothed(self):
        # 0 → 100KB at t=0, 200KB at t=1, 300KB at t=2. Window = 2s,
        # delta = 200KB → 100 KB/s averaged.
        samples = [(0.0, 100_000), (1.0, 200_000), (2.0, 300_000)]
        rate = _mgr._compute_rate(samples)
        assert abs(rate - 100_000) < 1e-6

    def test_record_byte_sample_trims_to_window(self, monkeypatch):
        # Force monotonic so we can place samples deterministically.
        clock = [100.0]
        monkeypatch.setattr(_mgr.time, "monotonic", lambda: clock[0])

        _mgr._record_byte_sample("t1", 1000)
        clock[0] = 101.0
        _mgr._record_byte_sample("t1", 2000)
        clock[0] = 105.0  # > _RATE_WINDOW_S (3 s) past the first
        _mgr._record_byte_sample("t1", 3000)

        # First sample (t=100) should have been trimmed.
        samples = _mgr._rates.get("t1") or []
        times = [s[0] for s in samples]
        assert 100.0 not in times
        # Last sample is present.
        assert samples[-1] == (105.0, 3000)


# ── Aggregate speed across concurrent jobs ─────────────────────────────────


class TestAggregateSpeed:
    def test_two_jobs_sum_to_total_rate(self, fake_settings, bus_spy, no_timer):
        # Stage two active jobs each pumping 1 MB/s.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 1_048_576,
        }
        _mgr._jobs["t2"] = {
            "item": {"Id": "t2"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 1_048_576,
        }
        _mgr._active.update({"t1", "t2"})
        _mgr._session_total = 2
        _mgr._rates["t1"] = [(0.0, 0), (1.0, 1_048_576)]
        _mgr._rates["t2"] = [(0.0, 0), (1.0, 1_048_576)]

        _mgr._stats_tick()

        stats = [e for e in bus_spy if e[0] == "stats"][-1]
        active, total_session, speed_bps, _eta = stats[1]
        assert active == 2
        assert total_session == 2
        # Two jobs at 1 MB/s each → ~2 MB/s.
        assert abs(speed_bps - 2 * 1_048_576) < 1e-3


# ── ETA semantics ───────────────────────────────────────────────────────────


class TestEta:
    def test_known_total_and_rate_yields_seconds(
        self, fake_settings, bus_spy, no_timer
    ):
        # 10 MB total, 1 MB downloaded → 9 MB remaining at 1 MB/s = 9s.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 10_485_760,
            "got_bytes": 1_048_576,
        }
        _mgr._active.add("t1")
        _mgr._session_total = 1
        _mgr._rates["t1"] = [(0.0, 0), (1.0, 1_048_576)]

        _mgr._stats_tick()
        _, _, _, eta = [e for e in bus_spy if e[0] == "stats"][-1][1]
        assert abs(eta - 9.0) < 1e-3

    def test_unknown_total_emits_minus_one(
        self, fake_settings, bus_spy, no_timer
    ):
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,  # no Content-Length
            "got_bytes": 1_048_576,
        }
        _mgr._active.add("t1")
        _mgr._session_total = 1
        _mgr._rates["t1"] = [(0.0, 0), (1.0, 1_048_576)]

        _mgr._stats_tick()
        _, _, _, eta = [e for e in bus_spy if e[0] == "stats"][-1][1]
        assert eta == -1.0

    def test_eta_over_twelve_hours_emits_minus_one(
        self, fake_settings, bus_spy, no_timer
    ):
        # 1 TB remaining at 1 KB/s → ~31 years. Hard cap kicks in.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 1_000_000_000_000,
            "got_bytes": 0,
        }
        _mgr._active.add("t1")
        _mgr._session_total = 1
        # 1 KB/s rate.
        _mgr._rates["t1"] = [(0.0, 0), (1.0, 1024)]

        _mgr._stats_tick()
        _, _, _, eta = [e for e in bus_spy if e[0] == "stats"][-1][1]
        assert eta == -1.0

    def test_longest_job_wins_eta(self, fake_settings, bus_spy, no_timer):
        # Job A: 9 MB remaining at 1 MB/s = 9s
        # Job B: 30 MB remaining at 1 MB/s = 30s
        # Aggregate ETA is the longer of the two.
        _mgr._jobs["a"] = {
            "item": {"Id": "a"},
            "parents": set(),
            "total_bytes": 10_485_760,
            "got_bytes": 1_048_576,
        }
        _mgr._jobs["b"] = {
            "item": {"Id": "b"},
            "parents": set(),
            "total_bytes": 31_457_280,
            "got_bytes": 1_048_576,
        }
        _mgr._active.update({"a", "b"})
        _mgr._session_total = 2
        _mgr._rates["a"] = [(0.0, 0), (1.0, 1_048_576)]
        _mgr._rates["b"] = [(0.0, 0), (1.0, 1_048_576)]

        _mgr._stats_tick()
        _, _, _, eta = [e for e in bus_spy if e[0] == "stats"][-1][1]
        # 30 MB total - 1 MB got = 29 MB remaining at 1 MB/s ≈ 29s —
        # that's the longest job (vs. job a's ~9s).
        assert abs(eta - 29.0) < 0.5


# ── Drain-edge detection + notification ────────────────────────────────────


class _FakeStore:
    """Stub the store calls _finish makes so the success branch can run
    without touching the filesystem."""

    def commit_blob(self, *a, **k):
        pass

    def discard_part(self, *a, **k):
        pass


class _FakeIndex:
    """Stub the index calls _finish makes so the test doesn't need a DB."""

    def __init__(self):
        self.states = {}

    def clear_retry(self, *a, **k):
        pass

    def set_state(self, item_id, state):
        self.states[item_id] = state

    def record_failure(self, *a, **k):
        pass

    def get_node(self, item_id):
        return None

    def parents(self, item_id):
        return []


@pytest.fixture
def stub_finish_deps(monkeypatch):
    """Replace ``offline.store`` / ``offline.index`` import targets so
    _finish can run end-to-end without a real DB."""
    fake_store = _FakeStore()
    fake_index = _FakeIndex()
    from modules.offline import store as store_mod
    from modules.offline import index as index_mod

    monkeypatch.setattr(store_mod, "commit_blob", fake_store.commit_blob)
    monkeypatch.setattr(store_mod, "discard_part", fake_store.discard_part)
    monkeypatch.setattr(index_mod, "clear_retry", fake_index.clear_retry)
    monkeypatch.setattr(index_mod, "set_state", fake_index.set_state)
    monkeypatch.setattr(index_mod, "record_failure", fake_index.record_failure)
    monkeypatch.setattr(index_mod, "get_node", fake_index.get_node)
    monkeypatch.setattr(index_mod, "parents", fake_index.parents)
    monkeypatch.setattr(index_mod, "recompute_state", lambda *a, **k: None)
    return fake_store, fake_index


class TestDrainEdge:
    def test_drain_fires_notification_once(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        # Simulate the last in-flight job finishing. We can't easily
        # round-trip a successful commit_blob without staging a real
        # .part file, so route the terminal through the failure branch
        # and validate the drain edge alone.
        _mgr._session_total = 1
        _mgr._session_failed = 0
        _mgr._active.add("t1")
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._finish("t1", success=False)
        # Drain edge fires exactly once: _active + _queue are empty and
        # _session_total > 0.
        assert len(notify_spy) == 1
        # One dispatched, one failed → all-failed body.
        assert notify_spy[0]["title"] == "Downloads failed"
        assert "1 download failed" in notify_spy[0]["body"]

    def test_drain_resets_session_counters(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 3
        _mgr._session_failed = 0
        _mgr._emit_drain_complete()
        assert _mgr._session_total == 0
        assert _mgr._session_failed == 0

    def test_drain_emits_final_zero_stats(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 2
        _mgr._session_failed = 0
        _mgr._emit_drain_complete()
        # Final emission carries (0, 0, 0.0, 0.0) so UI hides.
        stats = [e for e in bus_spy if e[0] == "stats"]
        assert stats[-1] == ("stats", (0, 0, 0.0, 0.0))

    def test_drain_respects_notify_disabled_setting(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        fake_settings.notify_on_download_complete = False
        _mgr._session_total = 2
        _mgr._session_failed = 0
        _mgr._emit_drain_complete()
        # No notify call, but state still resets and final stats still emit.
        assert notify_spy == []
        assert _mgr._session_total == 0
        stats = [e for e in bus_spy if e[0] == "stats"]
        assert stats[-1] == ("stats", (0, 0, 0.0, 0.0))

    def test_drain_all_failed_uses_failure_body(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 3
        _mgr._session_failed = 3
        _mgr._emit_drain_complete()
        assert len(notify_spy) == 1
        # All-failed copy keys off "Downloads failed".
        assert notify_spy[0]["title"] == "Downloads failed"
        assert "3 downloads failed" in notify_spy[0]["body"]

    def test_drain_mixed_uses_mixed_body(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 5
        _mgr._session_failed = 1
        _mgr._emit_drain_complete()
        assert len(notify_spy) == 1
        # 4 succeeded + 1 failed.
        body = notify_spy[0]["body"]
        assert "4 tracks downloaded" in body
        assert "1 failed" in body

    def test_drain_all_succeeded_uses_complete_body(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 5
        _mgr._session_failed = 0
        _mgr._emit_drain_complete()
        assert notify_spy[0]["title"] == "Downloads complete"
        assert "5 tracks downloaded" in notify_spy[0]["body"]

    def test_drain_singular_phrasing(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        _mgr._session_total = 1
        _mgr._session_failed = 0
        _mgr._emit_drain_complete()
        # Singular "1 track downloaded." — no plural s.
        assert "1 track downloaded" in notify_spy[0]["body"]
        assert "tracks" not in notify_spy[0]["body"]


# ── Page-opened-mid-flight prime ──────────────────────────────────────────


class TestGetQueueStats:
    def test_snapshot_matches_tick_payload(self, fake_settings, bus_spy, no_timer):
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 10_485_760,
            "got_bytes": 1_048_576,
        }
        _mgr._active.add("t1")
        _mgr._session_total = 3
        _mgr._rates["t1"] = [(0.0, 0), (1.0, 1_048_576)]

        snapshot = _mgr.get_queue_stats()
        active, total_session, speed_bps, eta = snapshot
        assert active == 1
        assert total_session == 3
        assert abs(speed_bps - 1_048_576) < 1e-3
        assert abs(eta - 9.0) < 1e-3

    def test_snapshot_idle_returns_zeros(self, fake_settings, bus_spy, no_timer):
        active, total_session, speed_bps, eta = _mgr.get_queue_stats()
        assert active == 0
        assert total_session == 0
        assert speed_bps == 0.0
        assert eta == -1.0

    def test_package_surface_exposes_get_queue_stats(self):
        import modules.offline as offline_pkg

        assert hasattr(offline_pkg, "get_queue_stats")
        # And it lands on __all__ for explicit re-export.
        assert "get_queue_stats" in offline_pkg.__all__


# ── Wi-Fi-only blocking + drain edge interaction ──────────────────────────


class TestWifiOnlyBlockingDrain:
    def test_drain_to_zero_active_while_queue_waits_no_drain_edge(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        """Spec §9: Wi-Fi-only + metered blocks dispatch. Active drains
        to zero (the in-flight jobs run to completion), but the queue
        still has waiting work — no drain-edge fires because we'd lose
        the notification when the gate later releases."""
        # User has Wi-Fi-only on and we're on metered.
        fake_settings.downloads_wifi_only = True
        _mgr._wifi_only = True
        _mgr._wifi_only_loaded = True
        _mgr.mark_metered(True)

        # One in-flight, one queued.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._jobs["t2"] = {
            "item": {"Id": "t2"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._active.add("t1")
        _mgr._queue.append("t2")
        _mgr._session_total = 2

        # Finish t1. _dispatch should NOT pop t2 (metered+wifi-only),
        # _maybe_drain_edge should NOT fire (queue isn't empty).
        _mgr._finish("t1", success=False)

        assert "t2" in _mgr._queue
        assert "t2" not in _mgr._active
        assert notify_spy == []

    def test_metered_releases_and_then_drains_fires_notification(
        self, fake_settings, bus_spy, notify_spy, no_timer, stub_finish_deps
    ):
        """Once the metered gate releases, queued work dispatches; when
        the last job lands, drain edge fires as normal."""
        fake_settings.downloads_wifi_only = True
        _mgr._wifi_only = True
        _mgr._wifi_only_loaded = True
        _mgr.mark_metered(True)

        # Stub _start_download — we drive the lifecycle manually.
        started = []

        def _fake_start(tid):
            started.append(tid)

        # Stage two waiting jobs.
        _mgr._jobs["t1"] = {
            "item": {"Id": "t1"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._jobs["t2"] = {
            "item": {"Id": "t2"},
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _mgr._queue.extend(["t1", "t2"])

        # Release the gate and patch _start_download just for this call.
        import unittest.mock as _mock

        with _mock.patch.object(_mgr, "_start_download", _fake_start):
            _mgr.mark_metered(False)
            # mark_metered kicks _dispatch which pops up to _MAX_CONCURRENT.
            assert sorted(started) == ["t1", "t2"]
            assert _mgr._session_total == 2

        # Now simulate t1 and t2 finishing successfully through _finish.
        # Use part_path=None which falls into the else branch — but the
        # drain edge fires regardless because _active + _queue empty.
        _mgr._finish("t1", success=False)
        # After t1 finishes, _active = {t2}, _queue = [], no drain yet.
        assert notify_spy == []
        _mgr._finish("t2", success=False)
        # Now drain edge fires.
        assert len(notify_spy) == 1
