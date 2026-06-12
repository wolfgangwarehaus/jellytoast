"""QtDecodeTap — THE visualizer audio source (all platforms).

An in-process QtMultimedia decode of the playing stream, consumer-paced
against mpv's position clock with wall-time extrapolation between
ticks. Serves every output mode — shared, bit-perfect, ALSA-direct —
identically. The Qt plumbing (download-complete body, QBuffer source)
is exercised live; these tests feed PCM straight into the hand-off
queue and pin the windowing + pacing + seek-signalling contract.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from jellytoast.visualizer import (  # noqa: E402
    _FFT_WINDOW,
    QtDecodeTap,
)


def _fake_np(url="http://srv/stream.flac?x=1", pos=42_000, duration=180_000):
    from types import SimpleNamespace

    return SimpleNamespace(stream_url=url, position=pos, duration=duration)


def test_engine_owns_single_parallel_tap(qapp, isolated_settings, monkeypatch):
    """One tap, every platform: the engine's only owned source is the
    parallel decode, fed directly to the FFT worker."""
    import jellytoast.visualizer as vis

    monkeypatch.setattr(vis, "get_now_playing", lambda: _fake_np())
    monkeypatch.setattr(vis.QtDecodeTap, "_begin_decode", lambda self, ms: None)
    engine = vis.VisualizerEngine()
    try:
        assert engine._owned_tap is engine._parallel_tap
        assert engine._tap is engine._parallel_tap
    finally:
        engine.stop(fast=True)


def test_engine_seeds_inflight_track_and_serves_on_start(
    qapp, isolated_settings, monkeypatch
):
    """Engine is lazy-built mid-track (visualizer opened during
    playback): it must seed source state from the live session and feed
    the tap at start(), or bars stay flat until the next track change."""
    import jellytoast.visualizer as vis

    fake = _fake_np()
    monkeypatch.setattr(vis, "get_now_playing", lambda: fake)
    monkeypatch.setattr(vis.QtDecodeTap, "_begin_decode", lambda self, ms: None)
    engine = vis.VisualizerEngine()
    try:
        assert engine._last_np is fake
        assert engine._last_pos_ms == 42_000
        engine.start()
        assert engine._parallel_tap._source == fake.stream_url
        assert engine._parallel_tap._target_ms == 42_000
    finally:
        engine.stop(fast=True)


# ── QtDecodeTap — the in-process default tap ────────────────────────────────
# Same pacing contract as the ffmpeg tap; the decode plumbing is Qt-side
# (covered by the live app), so these tests feed PCM straight into the
# hand-off queue and exercise windowing + pacing + seek signalling.


def _qtap(qapp, *, start_ms=0):

    clock = {"t": 100.0}
    t = QtDecodeTap(sample_rate=44100, now_fn=lambda: clock["t"])
    t._clock = clock  # test hook
    t._started = True
    t._source = "http://srv/stream"
    t._target_ms = start_ms
    t._anchor_samples = int(start_ms * 44.1)
    return t


def _feed(t, n_samples):
    arr = np.zeros(n_samples, dtype=np.float32)
    with t._lock:
        t._chunks.append(arr)
        t._buffered += n_samples


def test_qt_tap_assembles_windows_across_chunks(qapp):
    t = _qtap(qapp, start_ms=10_000)
    for n in (1000, 1000, 1000):  # 3000 samples in odd-sized chunks
        _feed(t, n)
    out = t()
    assert out is not None and len(out) == _FFT_WINDOW
    assert t._consumed == _FFT_WINDOW
    # remainder (3000 - 2048 = 952) must survive as the pending head
    assert t._pending is not None and t._pending.size == 952


def test_qt_tap_insufficient_buffer_returns_none(qapp):
    t = _qtap(qapp, start_ms=10_000)
    _feed(t, 100)
    assert t() is None


def test_qt_tap_holds_last_window_when_ahead(qapp):
    t = _qtap(qapp, start_ms=10_000)
    _feed(t, _FFT_WINDOW * 2)
    first = t()
    assert first is not None
    t._consumed = _FFT_WINDOW * 10  # force ahead-of-clock
    assert t() is first  # hold while playing
    t.set_paused(True)
    assert t() is None  # decay while paused


def test_qt_tap_extrapolates_clock(qapp):
    t = _qtap(qapp, start_ms=10_000)
    t.set_target_ms(10_000)
    _feed(t, _FFT_WINDOW * 20)
    t._consumed = _FFT_WINDOW * 10  # ahead of the frozen tick
    assert t() is None  # nothing cached, clock frozen
    t._clock["t"] = 100.6  # wall time advances, no position tick
    assert t() is not None


def test_qt_tap_seek_emits_single_restart_request(qapp):
    t = _qtap(qapp, start_ms=10_000)
    fired = []
    t._restart_requested.connect(lambda: fired.append(1))
    _feed(t, _FFT_WINDOW)
    assert t() is not None  # deliver once — arms the seek heuristic
    t.set_target_ms(90_000)  # a real forward seek, decode far behind
    t()
    t()
    t()
    # queued connection → the GUI slot hasn't run, so the pending guard
    # must keep this to ONE emission across repeated worker ticks
    assert t._restart_pending is True
    qapp.processEvents()  # deliver the queued signal
    assert len(fired) == 1


def test_qt_tap_forward_gap_before_first_delivery_does_not_restart(qapp):
    """A fresh decode that hasn't delivered yet is (probably) still
    downloading — the behind-the-clock gap is the download running
    long, and restarting would re-download from scratch in a loop."""
    t = _qtap(qapp, start_ms=0)
    t.set_target_ms(10_000)  # clock far ahead, nothing delivered yet
    assert t() is None
    assert t._restart_pending is False


def test_qt_tap_backward_seek_restarts_even_before_delivery(qapp):
    """An AHEAD gap can only be a genuine backward seek (a download
    lag always shows behind) — and it must restart, not hold: the hold
    branch would freeze the bars for the length of the jump."""
    t = _qtap(qapp, start_ms=120_000)  # decode anchored at 2:00
    t.set_target_ms(5_000)  # user seeks back to 0:05
    _feed(t, _FFT_WINDOW)
    t()
    assert t._restart_pending is True


def test_qt_tap_set_source_resets_state(qapp):
    t = _qtap(qapp, start_ms=0)
    _feed(t, _FFT_WINDOW * 3)
    assert t() is not None
    t._started = False  # block _begin_decode from touching Qt plumbing
    t.set_source("http://srv/next", start_ms=5_000)
    assert t._buffered == 0 and t._pending is None
    assert t._last_window is None
    assert t._target_ms == 5_000
    assert t._target_set_s > 0  # clock armed at push


def test_engine_default_tap_is_qt(qapp, isolated_settings, monkeypatch):
    import jellytoast.visualizer as vis

    monkeypatch.setattr(vis, "get_now_playing", lambda: _fake_np())
    monkeypatch.setattr(vis.QtDecodeTap, "_begin_decode", lambda self, ms: None)
    engine = vis.VisualizerEngine()
    try:
        assert isinstance(engine._parallel_tap, vis.QtDecodeTap)
        assert engine._tap is engine._parallel_tap
    finally:
        engine.stop(fast=True)


def test_qt_tap_extrapolation_freezes_while_paused(qapp):
    t = _qtap(qapp, start_ms=10_000)
    t.set_target_ms(10_000)
    t.set_paused(True)
    _feed(t, _FFT_WINDOW * 20)
    t._consumed = _FFT_WINDOW * 10
    t._clock["t"] = 100.6
    assert t() is None  # paused: target frozen at the last tick


def test_qt_tap_extrapolation_is_capped(qapp):
    """A stalled position feed (not paused) must not run away — the
    extrapolated clock stops _EXTRAPOLATE_CAP_S past the last tick."""
    t = _qtap(qapp, start_ms=10_000)
    t.set_target_ms(10_000)
    _feed(t, _FFT_WINDOW)
    assert t() is not None  # deliver once so the seek heuristic arms
    t._consumed = int(3.0 * 44100)  # ~3s ahead; cap is 2s
    t._clock["t"] = 100.0 + 120.0
    out = t()
    assert out is t._last_window  # held, never read past the cap
    assert t._restart_pending is False  # ahead-but-capped isn't a seek


def test_qt_tap_resume_reanchors_extrapolation(qapp):
    """Pause wall-time must not count as playback progress."""
    t = _qtap(qapp, start_ms=10_000)
    t.set_target_ms(10_000)
    t.set_paused(True)
    t._clock["t"] = 100.0 + 60.0  # a minute paused
    t.set_paused(False)  # re-anchors at t=160
    _feed(t, _FFT_WINDOW * 20)
    t._consumed = _FFT_WINDOW * 10  # ~0.46s ahead
    held = t()
    assert held is None or held is t._last_window  # no fresh read yet
    t._clock["t"] = 160.6
    assert t() is not None  # 600ms after resume → aligned again


def test_qt_tap_live_mode_skips_sync(qapp):
    t = _qtap(qapp, start_ms=0)
    t._live = True
    t._target_ms = -1
    _feed(t, _FFT_WINDOW)
    assert t() is not None


def test_qt_tap_clear_drops_source(qapp):
    t = _qtap(qapp, start_ms=0)
    _feed(t, _FFT_WINDOW)
    t.clear()
    assert t._source == ""
    assert t() is None
