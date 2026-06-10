"""Tests for the smart-retry backoff on the download manager.

A re-failed item shouldn't bounce back through ``retry_failed``
instantly. The manager stamps a ``retry_after_ts`` window on each
failure using an exponential schedule (``2 ** min(n, 6) * 30`` seconds),
and ``retry_failed`` filters out items still inside that window. A
successful download resets the bookkeeping so a future failure starts
fresh. ``get_retry_state`` is the read-only UI surface.
"""

from __future__ import annotations

import pytest

from jellytoast.offline import db as _db
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
    """Stop _dispatch so re-queued jobs don't fire real downloads."""
    monkeypatch.setattr(_mgr, "_dispatch", lambda: None)


@pytest.fixture
def frozen_time(monkeypatch):
    """Pin ``time.time()`` inside the manager module to a controlled
    epoch so tests can reason about exact ``retry_after_ts`` values."""
    state = {"now": 1_000_000}

    def _now():
        return state["now"]

    monkeypatch.setattr(_mgr.time, "time", _now)
    return state


def _add(item_id, kind, state, metadata=None):
    meta = metadata or {"Id": item_id, "Name": item_id}
    _index.upsert_node(item_id, kind, meta, requested=False, state=state)


class TestBackoffSchedule:
    @pytest.mark.parametrize(
        "count,seconds",
        [
            (1, 30),  # 2**0 * 30
            (2, 60),  # 2**1 * 30
            (3, 120),  # 2**2 * 30
            (4, 240),  # 2**3 * 30
            (5, 480),  # 2**4 * 30
            (6, 960),  # 2**5 * 30
            (7, 1920),  # 2**6 * 30 — at cap
            (8, 1920),  # cap holds
            (50, 1920),  # cap holds forever
        ],
    )
    def test_backoff_for(self, count, seconds):
        assert _mgr.backoff_for(count) == seconds

    def test_constants_documented(self):
        assert _mgr._BACKOFF_BASE_S == 30
        assert _mgr._BACKOFF_MAX_EXP == 6


class TestSchemaMigration:
    def test_nodes_has_retry_columns(self, offline_db):
        rows = _db.query("PRAGMA table_info(nodes)")
        cols = {r["name"] for r in rows}
        assert "retry_count" in cols
        assert "retry_after_ts" in cols

    def test_existing_rows_get_defaults(self, offline_db):
        _add("t1", "track", "pending")
        row = _index.get_node("t1")
        assert row["retry_count"] == 0
        assert row["retry_after_ts"] is None


class TestRecordFailure:
    def test_first_failure_sets_count_one_and_30s_window(
        self,
        offline_db,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        row = _index.get_node("t1")
        assert row["state"] == "failed"
        assert row["retry_count"] == 1
        assert row["retry_after_ts"] == frozen_time["now"] + 30

    def test_second_failure_sets_count_two_and_60s_window(
        self,
        offline_db,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        _mgr._record_failure("t1")
        row = _index.get_node("t1")
        assert row["retry_count"] == 2
        assert row["retry_after_ts"] == frozen_time["now"] + 60

    def test_third_failure_sets_count_three_and_120s_window(
        self,
        offline_db,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        for _ in range(3):
            _mgr._record_failure("t1")
        row = _index.get_node("t1")
        assert row["retry_count"] == 3
        assert row["retry_after_ts"] == frozen_time["now"] + 120


class TestClearRetryOnSuccess:
    def test_clear_retry_resets_count_and_ts(self, offline_db, frozen_time):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        _mgr._record_failure("t1")
        # Now imagine the next attempt succeeds.
        _index.clear_retry("t1")
        row = _index.get_node("t1")
        assert row["retry_count"] == 0
        assert row["retry_after_ts"] is None


class TestRetryFailedRespectsBackoff:
    def test_skips_items_still_in_backoff(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")  # 30s window
        # Time hasn't advanced — t1 is still in backoff.
        count = _mgr.retry_failed()
        assert count == 0
        assert _index.get_node("t1")["state"] == "failed"
        assert "t1" not in _mgr._queue

    def test_includes_items_whose_window_elapsed(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")  # window ends at now+30
        frozen_time["now"] += 31  # past the window
        count = _mgr.retry_failed()
        assert count == 1
        assert _index.get_node("t1")["state"] == "pending"
        assert "t1" in _mgr._queue

    def test_includes_items_at_exact_boundary(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        # retry_after_ts == now is "no longer in the future" — eligible.
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        frozen_time["now"] += 30
        count = _mgr.retry_failed()
        assert count == 1

    def test_force_bypasses_backoff(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")  # still inside the window
        count = _mgr.retry_failed(force=True)
        assert count == 1
        assert _index.get_node("t1")["state"] == "pending"
        assert "t1" in _mgr._queue

    def test_legacy_rows_with_null_window_are_eligible(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        # Pre-migration rows: state='failed' but retry_after_ts is NULL.
        _add("t1", "track", "failed")
        assert _index.get_node("t1")["retry_after_ts"] is None
        count = _mgr.retry_failed()
        assert count == 1

    def test_mixed_eligible_and_backed_off(
        self,
        offline_db,
        fake_settings,
        bus_spy,
        no_dispatch,
        frozen_time,
    ):
        _add("ready", "track", "pending")
        _add("waiting", "track", "pending")
        _mgr._record_failure("ready")
        _mgr._record_failure("waiting")
        # Advance past ready's window only; waiting still inside.
        frozen_time["now"] += 31
        # But waiting was just failed at the new time too — push its
        # window further so it's still inside.
        _mgr._record_failure("waiting")  # second failure -> 60s
        count = _mgr.retry_failed()
        assert count == 1
        assert "ready" in _mgr._queue
        assert "waiting" not in _mgr._queue


class TestGetRetryState:
    def test_returns_none_for_unknown_item(self, offline_db):
        assert _mgr.get_retry_state("ghost") is None

    def test_returns_none_for_never_failed_item(self, offline_db):
        _add("t1", "track", "complete")
        assert _mgr.get_retry_state("t1") is None

    def test_returns_state_for_failed_item(self, offline_db, frozen_time):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        state = _mgr.get_retry_state("t1")
        assert state is not None
        assert state["retry_count"] == 1
        assert state["retry_after_ts"] == frozen_time["now"] + 30
        assert state["seconds_until_retry"] == 30

    def test_seconds_until_retry_decreases(self, offline_db, frozen_time):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        frozen_time["now"] += 10
        state = _mgr.get_retry_state("t1")
        assert state["seconds_until_retry"] == 20

    def test_seconds_until_retry_clamps_at_zero(self, offline_db, frozen_time):
        _add("t1", "track", "pending")
        _mgr._record_failure("t1")
        frozen_time["now"] += 999
        state = _mgr.get_retry_state("t1")
        assert state["seconds_until_retry"] == 0
