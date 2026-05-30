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


# ── Scrobble/shutdown lifecycle hardening (2026-05-28 audit cluster) ──────────


def _make_eligible(m: ScrobbleManager, item_id: str = "t1", dur: int = 200_000):
    """Play a track and drive it past the eligibility threshold."""
    _play(m, _np(item_id=item_id, duration_ms=dur), dur)
    for ms in range(0, dur // 2 + 30_000, 1000):
        _tick(m, ms)
    assert m._current is not None and m._current.eligible is True
    return m._current


class TestFlushPendingOfflineGate:
    """flush_pending must NOT phone home while offline mode is on (it fires
    at startup + every connectivity edge)."""

    def test_offline_skips_flush(self, manager, monkeypatch):
        calls = []
        monkeypatch.setattr(manager, "_flush_listenbrainz_async", lambda: calls.append("lb"))
        monkeypatch.setattr(manager, "_flush_lastfm_async", lambda: calls.append("lf"))
        manager._settings.offline_mode = True
        manager.flush_pending()
        assert calls == []

    def test_online_runs_flush(self, manager, monkeypatch):
        calls = []
        monkeypatch.setattr(manager, "_flush_listenbrainz_async", lambda: calls.append("lb"))
        monkeypatch.setattr(manager, "_flush_lastfm_async", lambda: calls.append("lf"))
        manager._settings.offline_mode = False
        manager.flush_pending()
        assert calls == ["lb", "lf"]


class TestFlushRemovesScannedPrefix:
    """On a successful flush, the whole scanned slice is removed — not just
    the well-formed count — so a malformed early entry can't shift removal
    and leave a sent entry behind to re-send."""

    def test_remove_drops_scanned_not_wellformed_count(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        manager._settings.listenbrainz_token = "tok"
        pending = [
            {"service": "listenbrainz", "junk": 1},  # malformed → filtered out of listens
            {"service": "listenbrainz", "track_metadata": {"a": 1}, "listened_at": 100},
            {"service": "listenbrainz", "track_metadata": {"b": 2}, "listened_at": 200},
        ]
        monkeypatch.setattr(mgr_mod.scrobble_queue, "pending", lambda svc="": list(pending))
        removed = []
        monkeypatch.setattr(
            mgr_mod.scrobble_queue,
            "remove",
            lambda svc, count=0, records=None: removed.append((svc, records)),
        )

        def _ra(fn, *a, on_result=None, on_error=None, **k):
            if on_result:
                on_result(True)

        monkeypatch.setattr(mgr_mod, "run_async", _ra)
        manager._flush_listenbrainz_async()
        # 2 well-formed sent, but the full 3-entry scanned span is removed —
        # now by IDENTITY (the exact records), not the oldest-N by count.
        assert len(removed) == 1
        assert removed[0][0] == "listenbrainz"
        assert removed[0][1] == pending


class TestFlushCurrentOnQuit:
    """The in-flight eligible track is persisted synchronously at quit
    (window-close + tray paths) so the dying async submit can't lose it."""

    def test_enqueues_eligible_current(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        added = []
        monkeypatch.setattr(mgr_mod.scrobble_queue, "add", lambda svc, rec: added.append(svc))
        _make_eligible(manager)
        assert manager._current.scrobbled is False
        manager.flush_current_on_quit()
        assert manager._current.scrobbled is True
        assert "listenbrainz" in added

    def test_noop_when_not_eligible(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        added = []
        monkeypatch.setattr(mgr_mod.scrobble_queue, "add", lambda svc, rec: added.append(svc))
        _play(manager, _np(item_id="t1", duration_ms=200_000), 200_000)  # no ticks → not eligible
        manager.flush_current_on_quit()
        assert added == []

    def test_noop_when_already_scrobbled(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        added = []
        monkeypatch.setattr(mgr_mod.scrobble_queue, "add", lambda svc, rec: added.append(svc))
        _make_eligible(manager)
        manager._current.scrobbled = True
        manager.flush_current_on_quit()
        assert added == []


class TestCastHandoffNoDoubleScrobble:
    """Casting a track that was already scrobbled must not re-arm + double
    count it when the cast path re-emits playback_started to re-render."""

    def test_handoff_preserves_scrobbled_flag(self, manager):
        _make_eligible(manager, item_id="t1")
        manager._maybe_scrobble_current()  # scrobbles t1 (LB disabled → just flags state)
        assert manager._current.scrobbled is True
        assert manager._recently_scrobbled_item_id == "t1"
        # Cast handoff: the local stop cleared _current, then the cast path
        # flags the handoff and re-emits playback_started for the SAME track.
        manager._current = None
        manager.note_cast_handoff()
        manager._on_playback_started(_np(item_id="t1", duration_ms=200_000))
        assert manager._current is not None
        assert manager._current.scrobbled is True  # carried over — not re-armed

    def test_handoff_flag_is_one_shot_and_track_specific(self, manager):
        manager._recently_scrobbled_item_id = "t1"
        manager.note_cast_handoff()
        # A DIFFERENT track right after the flag is set must NOT be
        # suppressed, and the one-shot flag is consumed.
        manager._on_playback_started(_np(item_id="t2", duration_ms=200_000))
        assert manager._current.scrobbled is False
        assert manager._suppress_rescrobble_once is False


class TestMBIDExtraction:
    """#587: _extract_mbid must recover the MusicBrainz recording id from
    both providers. Jellyfin carries it in ProviderIds; Subsonic surfaces
    it on the RAW song under _subsonic_raw (the normalized dict drops it)."""

    def test_jellyfin_provider_ids(self):
        np = NowPlaying(item_id="j", raw={"ProviderIds": {"MusicBrainzRecording": "rec-1"}})
        assert mgr_mod._extract_mbid(np) == "rec-1"

    def test_jellyfin_track_fallback(self):
        np = NowPlaying(item_id="j", raw={"ProviderIds": {"MusicBrainzTrack": "trk-2"}})
        assert mgr_mod._extract_mbid(np) == "trk-2"

    def test_subsonic_from_subsonic_raw(self):
        # The real Subsonic/Navidrome case: mbid nested under _subsonic_raw.
        np = NowPlaying(
            item_id="s",
            raw={"Id": "s", "_subsonic_raw": {"id": "s", "musicBrainzId": "mb-3"}},
        )
        assert mgr_mod._extract_mbid(np) == "mb-3"

    def test_missing_is_empty(self):
        assert mgr_mod._extract_mbid(NowPlaying(item_id="x", raw={})) == ""


class TestFlushInFlightGuard:
    """#437: a reconnect emits connectivity_changed AND offline_mode_changed,
    each calling flush_pending. A second async flush must not kick off while
    one is in flight (it would double-POST the same prefix)."""

    def test_second_flush_guarded_while_in_flight(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        manager._settings.listenbrainz_token = "tok"
        pending = [{"service": "listenbrainz", "track_metadata": {"a": 1}, "listened_at": 100}]
        monkeypatch.setattr(mgr_mod.scrobble_queue, "pending", lambda svc="": list(pending))
        calls = []
        # run_async that never calls back → the in-flight flag stays set.
        monkeypatch.setattr(mgr_mod, "run_async", lambda *a, **k: calls.append(1))

        manager._flush_listenbrainz_async()
        manager._flush_listenbrainz_async()  # in-flight → short-circuits
        assert len(calls) == 1

    def test_guard_clears_on_error_so_next_flush_runs(self, manager, monkeypatch):
        manager._settings.listenbrainz_enabled = True
        manager._settings.listenbrainz_token = "tok"
        pending = [{"service": "listenbrainz", "track_metadata": {"a": 1}, "listened_at": 100}]
        monkeypatch.setattr(mgr_mod.scrobble_queue, "pending", lambda svc="": list(pending))
        monkeypatch.setattr(mgr_mod.scrobble_queue, "remove", lambda *a, **k: None)
        calls = []

        def _ra(fn, *a, on_result=None, on_error=None, **k):
            calls.append(1)
            if on_error:
                on_error(RuntimeError("boom"))  # failure clears the guard

        monkeypatch.setattr(mgr_mod, "run_async", _ra)
        manager._flush_listenbrainz_async()
        manager._flush_listenbrainz_async()  # guard cleared by on_error → runs again
        assert len(calls) == 2
