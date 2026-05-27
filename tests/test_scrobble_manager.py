"""Tests for the scrobble eligibility rule.

This file covers *just* the eligibility math in ``ScrobbleManager``:

- the 30s minimum-duration gate
- the threshold = min(duration//2, 240_000) ms target
- the 5_000 ms per-tick cap that prevents a forward seek from counting

Network submission (``lastfm.py`` / ``listenbrainz.py``), the on-disk
queue (``queue.py``), and the bus wiring itself are exercised
elsewhere — here we drive the manager's slots directly with a synthetic
sequence of ``position_updated`` / ``playback_started`` / ``duration_set``
calls and assert against the per-track ``_TrackState`` it builds.

Approach:

The manager's ``__init__`` constructs a real ``PlayerBus`` (a QObject)
and wires slots to it. That works fine without a running QApplication
event loop — connecting signals is a pure-Python operation. We then
neutralise the network and settings side-effects by patching
``run_async`` to a no-op and stubbing the settings so the now-playing
fanout in ``_on_playback_started`` doesn't try to reach LB/LF.

No event loop is ever spun. We invoke the slot methods directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from modules.player_state import NowPlaying
from modules.scrobble import manager as mgr_mod
from modules.scrobble.manager import (
    _MAX_ELIGIBILITY_MS,
    _MAX_TICK_DELTA_MS,
    _MIN_TRACK_DURATION_MS,
    ScrobbleManager,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@dataclass
class _StubSettings:
    """Drop-in replacement for ``Settings`` — just the bits the manager
    reads. All-disabled so the now-playing fanout in
    ``_on_playback_started`` is a no-op even if we hadn't also patched
    ``run_async``."""

    listenbrainz_enabled: bool = False
    listenbrainz_token: str = ""
    listenbrainz_url: str = ""
    lastfm_enabled: bool = False
    lastfm_session_key: str = ""
    offline_mode: bool = False


@pytest.fixture
def manager(monkeypatch):
    """Construct a ``ScrobbleManager`` with disabled-everything settings
    and a no-op ``run_async``. Any incidental network or queue writes
    that fall out of the eligibility-math paths get swallowed."""
    monkeypatch.setattr(mgr_mod, "run_async", lambda *a, **k: None)
    m = ScrobbleManager()
    m._settings = _StubSettings()
    return m


def _np(
    item_id: str = "track-1",
    duration_ms: int = 0,
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
) -> NowPlaying:
    """Build a NowPlaying with the minimum fields the manager needs to
    treat the track as scrobble-eligible candidate metadata. ``duration``
    is in ms (matches ``NowPlaying.duration``)."""
    return NowPlaying(
        item_id=item_id,
        title=title,
        subtitle=artist,
        album=album,
        duration=duration_ms,
        item_type="Audio",
    )


def _play(m: ScrobbleManager, np: NowPlaying, duration_ms: int | None = None):
    """Simulate a real playback start: ``playback_started`` then
    ``duration_set``. mpv emits them in that order in production."""
    m._on_playback_started(np)
    if duration_ms is not None:
        m._on_duration_set(duration_ms)


def _tick(m: ScrobbleManager, position_ms: int):
    """One simulated position_updated tick."""
    m._on_position_updated(position_ms)


# ── 1. Sub-30s duration: never eligible ──────────────────────────────────────


class TestShortTrackGate:
    def test_below_floor_never_eligible(self, manager):
        # A 29-second track is below the floor — even if the user
        # "plays" it for 60s of wall-clock (impossible, but illustrates
        # that the math doesn't gate on elapsed alone) it must not
        # flip eligible.
        np = _np(duration_ms=29_000)
        _play(manager, np, 29_000)
        for ms in range(0, 60_000, 200):
            _tick(manager, ms)
        assert manager._current is not None
        # Even with elapsed > duration the floor wins.
        assert manager._current.elapsed_ms > 29_000
        assert manager._current.eligible is False

    def test_floor_constant_is_30s(self):
        # Sanity-check the contract — the eligibility doc string says
        # "longer than 30s"; the code's gate uses > 30_000.
        assert _MIN_TRACK_DURATION_MS == 30_000

    def test_exactly_at_floor_not_eligible(self, manager):
        # A track of exactly 30_000 ms sits *at* the floor — per the
        # "longer than 30 s" rule it must not flip eligible no matter
        # how much elapsed accrues.
        np = _np(duration_ms=30_000)
        _play(manager, np, 30_000)
        for ms in range(0, 30_001, 200):
            _tick(manager, ms)
        assert manager._current.eligible is False


# ── 2-4. Threshold math (≥30s, 4-min track, 10-min track) ────────────────────


class TestThresholdMath:
    def test_just_above_floor(self, manager):
        # 30_001 ms is the smallest duration that clears the
        # "longer than 30 s" floor. min(15_000, 240_000) == 15_000.
        # Eligibility flips at 15s of forward play, not a tick before.
        np = _np(duration_ms=30_001)
        _play(manager, np, 30_001)
        # 14.8s elapsed → still not eligible.
        for ms in range(0, 14_800 + 1, 200):
            _tick(manager, ms)
        assert manager._current.eligible is False
        # Cross 15s.
        _tick(manager, 15_000)
        assert manager._current.eligible is True

    def test_four_minute_track(self, manager):
        # 4-minute track → threshold = min(120_000, 240_000) = 120_000.
        np = _np(duration_ms=240_000)
        _play(manager, np, 240_000)
        # At 119_800 ms: not yet.
        for ms in range(0, 119_800 + 1, 200):
            _tick(manager, ms)
        assert manager._current.eligible is False
        _tick(manager, 120_000)
        assert manager._current.eligible is True

    def test_ten_minute_track_caps_at_four_minutes(self, manager):
        # 10-minute track → duration//2 = 300_000 ms, but the cap
        # at 240_000 ms wins.
        np = _np(duration_ms=600_000)
        _play(manager, np, 600_000)
        assert manager._current.threshold_ms() == 240_000
        # Not eligible at 239.8s.
        for ms in range(0, 239_800 + 1, 200):
            _tick(manager, ms)
        assert manager._current.eligible is False
        _tick(manager, 240_000)
        assert manager._current.eligible is True

    def test_cap_constant(self):
        # The "or 4 minutes" half of the rule.
        assert _MAX_ELIGIBILITY_MS == 240_000

    def test_eight_minute_track_threshold_is_four_minutes(self, manager):
        # 8-minute track → duration//2 = 240_000 ms, equal to the cap.
        # min(240_000, 240_000) == 240_000.
        np = _np(duration_ms=480_000)
        _play(manager, np, 480_000)
        assert manager._current.threshold_ms() == 240_000


# ── 5. Forward play accumulates across pauses ───────────────────────────────


class TestAccumulationAcrossPauses:
    def test_resume_continues_count(self, manager):
        # Play 60s, "pause" (no further ticks), resume from 60s and
        # tick another 60s → elapsed should be 120s.
        # We're testing a 5-minute (300s) track so the threshold is
        # min(150s, 240s) = 150s and we don't accidentally cross it
        # during the first leg.
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        # Leg 1: 0 → 60s
        for ms in range(0, 60_001, 500):
            _tick(manager, ms)
        elapsed_after_leg1 = manager._current.elapsed_ms
        # ~60_000 — but the very first tick (0→0) contributes nothing
        # and last_position_ms is now at 60_000.
        assert elapsed_after_leg1 == pytest.approx(60_000, abs=500)
        assert manager._current.eligible is False

        # Pause window: no ticks. last_position_ms stays at 60_000.
        # Leg 2: resume — ticks continue from 60_000 forward.
        for ms in range(60_500, 120_001, 500):
            _tick(manager, ms)
        elapsed = manager._current.elapsed_ms
        # Cumulative ≈ 120s. Still below the 150s threshold.
        assert elapsed == pytest.approx(120_000, abs=1_000)
        assert manager._current.eligible is False


# ── 6. Seek > 5s does not count toward elapsed ───────────────────────────────


class TestSeekCap:
    def test_forward_seek_above_cap_is_dropped(self, manager):
        # Play 10s, then a single tick reporting position 70_000 (a
        # +60s seek), then 10 more seconds of play. The seek tick must
        # be skipped wholesale — its 60_000 delta exceeds the 5_000ms
        # cap. Elapsed should be 10 + 0 + 10 = 20s, NOT 80s.
        np = _np(duration_ms=600_000)  # long track to keep us under threshold
        _play(manager, np, 600_000)
        # Leg 1: 0 → 10_000.
        for ms in range(0, 10_001, 500):
            _tick(manager, ms)
        elapsed_a = manager._current.elapsed_ms
        # The seek tick: position jumps to 70_000.
        _tick(manager, 70_000)
        # Leg 2: 70_000 → 80_000.
        for ms in range(70_500, 80_001, 500):
            _tick(manager, ms)
        elapsed_b = manager._current.elapsed_ms
        # Strict: the only contributions are the two 10s legs.
        assert elapsed_a == pytest.approx(10_000, abs=500)
        assert elapsed_b == pytest.approx(20_000, abs=1_000)
        # Crucial: we did NOT count the 60s skip.
        assert elapsed_b < 30_000
        assert manager._current.eligible is False

    def test_cap_constant_is_5s(self):
        assert _MAX_TICK_DELTA_MS == 5_000

    def test_boundary_tick_at_cap_is_counted(self, manager):
        # The cap is *inclusive* on the upper end: ``0 < delta <= 5_000``.
        # A delta of exactly 5_000 ms counts toward elapsed — only
        # strictly greater deltas are treated as seeks.
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        _tick(manager, 0)
        _tick(manager, 5_000)
        assert manager._current.elapsed_ms == 5_000

    def test_boundary_tick_just_below_cap_is_counted(self, manager):
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        _tick(manager, 0)
        _tick(manager, 4_999)
        assert manager._current.elapsed_ms == 4_999

    def test_boundary_tick_just_above_cap_is_dropped(self, manager):
        # 5_001 ms is one beyond the cap — clearly a seek, dropped.
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        _tick(manager, 0)
        _tick(manager, 5_001)
        assert manager._current.elapsed_ms == 0

    def test_zero_delta_not_counted(self, manager):
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        _tick(manager, 5_000)
        # Re-fire the exact same position — delta == 0, dropped.
        _tick(manager, 5_000)
        # The first tick from 0 → 5_000 counted; the second was a no-op.
        assert manager._current.elapsed_ms == 5_000

    def test_negative_delta_not_counted(self, manager):
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        _tick(manager, 5_000)
        # Backward tick — delta < 0, dropped.
        _tick(manager, 4_000)
        assert manager._current.elapsed_ms == 5_000


# ── 7. Backward seek doesn't subtract from elapsed ──────────────────────────


class TestBackwardSeek:
    def test_backward_jump_is_ignored(self, manager):
        # Play to 20s, then seek back to 5s. The negative delta must
        # not subtract from elapsed — elapsed should plateau, not
        # decrease.
        np = _np(duration_ms=300_000)
        _play(manager, np, 300_000)
        for ms in range(0, 20_001, 500):
            _tick(manager, ms)
        elapsed_before = manager._current.elapsed_ms
        # Big backward seek.
        _tick(manager, 5_000)
        elapsed_after = manager._current.elapsed_ms
        assert elapsed_after == elapsed_before
        # Subsequent forward ticks from the new (lower) position
        # contribute normally.
        for ms in range(5_500, 10_001, 500):
            _tick(manager, ms)
        assert manager._current.elapsed_ms > elapsed_before


# ── 8. Track change resets elapsed ──────────────────────────────────────────


class TestTrackChange:
    def test_new_track_starts_fresh(self, manager):
        # Play track A past its threshold, then switch to track B.
        # B's elapsed must start at 0 (and B must not inherit A's
        # eligibility).
        a = _np(item_id="a", duration_ms=30_001)
        _play(manager, a, 30_001)
        for ms in range(0, 16_001, 500):
            _tick(manager, ms)
        assert manager._current.eligible is True
        a_id_before = manager._current.np.item_id
        assert a_id_before == "a"

        # Switch to track B.
        b = _np(item_id="b", duration_ms=240_000)
        _play(manager, b, 240_000)
        # Now-tracked state is B with zero elapsed.
        assert manager._current.np.item_id == "b"
        assert manager._current.elapsed_ms == 0
        assert manager._current.eligible is False
        # A tick at the *previous* position number (50_000 say) is now
        # the very first tick for B; delta from 0 to 50_000 is way
        # above the cap → dropped, elapsed remains 0.
        _tick(manager, 50_000)
        assert manager._current.elapsed_ms == 0

    def test_same_track_replay_does_not_reset(self, manager):
        # ``playback_started`` for the same item_id is treated as a
        # resume — the elapsed counter must not zero out.
        a = _np(item_id="a", duration_ms=300_000)
        _play(manager, a, 300_000)
        for ms in range(0, 30_001, 500):
            _tick(manager, ms)
        elapsed_before = manager._current.elapsed_ms
        # Re-fire playback_started for the same id (e.g. saved-position
        # restore).
        manager._on_playback_started(a)
        assert manager._current.elapsed_ms == elapsed_before


# ── 9. Position update before duration_set is benign ────────────────────────


class TestPositionBeforeDuration:
    def test_no_crash_no_eligibility_without_duration(self, manager):
        # NowPlaying with duration=0 (mpv hasn't told us yet). Lots
        # of ticks should accumulate elapsed but never flip eligible
        # because the duration is below the 30s floor.
        np = _np(item_id="late", duration_ms=0)
        manager._on_playback_started(np)
        assert manager._current is not None
        assert manager._current.duration_ms == 0
        # Drive a bunch of ticks before any duration_set.
        for ms in range(0, 60_001, 200):
            _tick(manager, ms)
        assert manager._current.eligible is False
        # When duration finally arrives, threshold becomes real and
        # the *already-accumulated* elapsed can satisfy it.
        manager._on_duration_set(120_000)
        # threshold = 60_000; elapsed accumulated above is ~60_000.
        # Next tick triggers the eligibility re-check.
        _tick(manager, 60_200)
        assert manager._current.eligible is True


# ── 10. Threshold edge case: exact equality flips eligible ──────────────────


class TestThresholdEquality:
    def test_elapsed_equal_to_threshold_is_eligible(self, manager):
        # The code uses ``elapsed_ms >= threshold_ms()`` — equality
        # counts.
        np = _np(duration_ms=60_000)  # threshold = 30_000
        _play(manager, np, 60_000)
        # Land elapsed at exactly 30_000 via 200ms ticks.
        for ms in range(0, 30_001, 200):
            _tick(manager, ms)
        assert manager._current.elapsed_ms == 30_000
        assert manager._current.threshold_ms() == 30_000
        assert manager._current.eligible is True

    def test_elapsed_one_below_threshold_not_eligible(self, manager):
        np = _np(duration_ms=60_000)
        _play(manager, np, 60_000)
        # Land at exactly 29_800 — one tick (200ms) below threshold.
        for ms in range(0, 29_801, 200):
            _tick(manager, ms)
        assert manager._current.elapsed_ms == 29_800
        assert manager._current.eligible is False
