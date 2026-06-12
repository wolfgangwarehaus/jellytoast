"""ParallelDecodeTap — THE visualizer audio source (all platforms).

A second, analysis-only ffmpeg decode of the playing stream,
consumer-paced against mpv's position clock with wall-time
extrapolation between ticks. Serves every output mode — shared,
bit-perfect, ALSA-direct — identically.
docs/research/visualizer_bit_perfect_2026-06-11.md
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from jellytoast.visualizer import (  # noqa: E402
    _FFT_WINDOW,
    ParallelDecodeTap,
)

_WIN_BYTES = _FFT_WINDOW * 4


class _FakePipe:
    """Endless raw f32 stream; counts bytes handed out."""

    def __init__(self):
        self.served = 0

    def read(self, n):
        self.served += n
        return b"\x00" * n

    def close(self):
        pass


class _FakeProc:
    def __init__(self):
        self.stdout = _FakePipe()
        self.killed = False

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _tap(*, start_ms=0, live=False, now=None):
    clock = {"t": 100.0}
    t = ParallelDecodeTap(sample_rate=44100, now_fn=lambda: clock["t"])
    t._clock = clock  # test hook
    t.start()
    t._source = "http://srv/stream"
    t._live = live
    t._target_ms = start_ms
    # install the fake proc directly — _spawn is exercised separately
    t._proc = _FakeProc()
    t._anchor_samples = int(start_ms * 44.1)
    t._consumed = 0
    return t


def test_aligned_read_returns_window_and_advances():
    t = _tap(start_ms=10_000)
    out = t()
    assert out is not None and len(out) == _FFT_WINDOW
    assert t._consumed == _FFT_WINDOW


def test_ahead_of_clock_returns_none():
    """Paused playback: the target stalls while the decode is ahead —
    no read, engine paints zeros."""
    t = _tap(start_ms=10_000)
    t._consumed = _FFT_WINDOW * 10  # decode got ahead
    assert t() is None
    assert t._proc.stdout.served == 0


def test_small_backlog_discards_then_reads():
    t = _tap(start_ms=10_000)
    # decode is ~6 windows behind the clock
    t._target_ms = 10_000 + int(6 * _FFT_WINDOW / 44.1)
    out = t()
    assert out is not None
    # served more than one window's worth (discard + the returned one)
    assert t._proc.stdout.served > _WIN_BYTES


def test_big_gap_respawns_at_target():
    t = _tap(start_ms=10_000)
    spawns = []
    t._spawn = lambda s: spawns.append(s) or setattr(t, "_proc", _FakeProc())
    t._target_ms = 90_000  # a real seek
    t()
    assert spawns and abs(spawns[0] - 90.0) < 0.2


def test_clock_extrapolates_between_position_ticks():
    """Position ticks are discrete but playback isn't: with the decode
    ahead of the LAST tick, an advancing wall clock must keep reads
    flowing (frozen-target gating made the wave jitter between band
    bursts and zero-emission decays)."""
    t = _tap(start_ms=10_000)
    t.set_target_ms(10_000)  # stamps _target_set_s at clock t=100.0
    t._consumed = _FFT_WINDOW * 10  # decode ~0.46s ahead of the tick
    assert t() is None  # clock hasn't advanced — still ahead
    t._clock["t"] = 100.6  # 600ms of wall time, no new position tick
    out = t()
    assert out is not None and len(out) == _FFT_WINDOW


def test_extrapolation_freezes_while_paused():
    t = _tap(start_ms=10_000)
    t.set_target_ms(10_000)
    t.set_paused(True)
    t._consumed = _FFT_WINDOW * 10
    t._clock["t"] = 100.6
    assert t() is None  # paused: target frozen at the last tick


def test_extrapolation_is_capped():
    """A stalled position feed (not paused) must not run away — the
    extrapolated clock stops _EXTRAPOLATE_CAP_S past the last tick."""
    t = _tap(start_ms=10_000)
    t.set_target_ms(10_000)
    # Decode sits ~3s ahead; cap is 2s, so even minutes of wall time
    # must not unlock reads.
    t._consumed = int(3.0 * 44100)
    t._clock["t"] = 100.0 + 120.0
    assert t() is None


def test_resume_reanchors_extrapolation():
    """Pause wall-time must not count as playback progress."""
    t = _tap(start_ms=10_000)
    t.set_target_ms(10_000)
    t.set_paused(True)
    t._clock["t"] = 100.0 + 60.0  # a minute paused
    t.set_paused(False)  # re-anchors at t=160
    t._consumed = _FFT_WINDOW * 10  # ~0.46s ahead
    assert t() is None  # no progress since resume → still ahead
    t._clock["t"] = 160.6
    assert t() is not None  # 600ms after resume → aligned again


def test_ahead_while_playing_holds_last_window():
    """Window (46 ms) > FFT tick (33 ms), so some ticks legitimately
    have no fresh window. While PLAYING those must re-serve the last
    window — a None paints zero bands and the wave flickers."""
    t = _tap(start_ms=10_000)
    first = t()  # aligned read caches the window
    assert first is not None
    t._consumed = _FFT_WINDOW * 10  # now ahead of the frozen clock
    held = t()
    assert held is not None
    assert held is first  # the cached window, not a fresh read


def test_ahead_while_paused_returns_none_despite_cache():
    """Paused must decay to baseline — no hold."""
    t = _tap(start_ms=10_000)
    assert t() is not None  # cache a window
    t.set_paused(True)
    t._consumed = _FFT_WINDOW * 10
    assert t() is None


def test_set_source_starts_the_clock():
    """Track start: set_source alone must arm extrapolation — waiting
    for the first position tick left ~a second of dead bars."""
    t = _tap(start_ms=0)
    t.set_source("http://srv/stream", start_ms=0)
    t._proc = _FakeProc()  # set_source killed the fixture's proc
    t._anchor_samples = 0
    t._consumed = _FFT_WINDOW * 10  # decode ~0.46s ahead of start_ms
    assert t() is None  # clock hasn't advanced, nothing cached yet
    t._clock["t"] = 100.6  # 600ms of wall time, still no position tick
    out = t()
    assert out is not None and len(out) == _FFT_WINDOW


def test_live_mode_skips_sync_entirely():
    t = _tap(start_ms=0, live=True)
    t._target_ms = -1
    out = t()
    assert out is not None and len(out) == _FFT_WINDOW


def test_not_started_or_sourceless_is_inert():
    t = ParallelDecodeTap(sample_rate=44100)
    assert t() is None  # never started
    t.start()
    assert t() is None  # no source


def test_set_source_strips_file_scheme():
    t = ParallelDecodeTap(sample_rate=44100)
    t.start()
    t.set_source("file:///home/u/Music/a%20b.flac", start_ms=0)
    assert t._source == "/home/u/Music/a b.flac"


def test_clear_drops_source():
    t = _tap()
    t.clear()
    assert t._source == ""
    assert t() is None


def test_missing_ffmpeg_is_inert(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _b: None)
    t = ParallelDecodeTap(sample_rate=44100, now_fn=lambda: 100.0)
    t.start()
    t.set_source("http://srv/x", start_ms=0)
    assert t() is None  # spawn fails quietly


def test_build_cmd_shape(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/ffmpeg")
    t = ParallelDecodeTap(sample_rate=44100)
    t._source = "http://srv/x"
    cmd = t._build_cmd(12.5)
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-ss" in cmd and "12.500" in cmd
    assert "f32le" in cmd and "pipe:1" in cmd
    t._live = True
    assert "-ss" not in t._build_cmd(12.5)


def _fake_np(url="http://srv/stream.flac?x=1", pos=42_000, duration=180_000):
    from types import SimpleNamespace

    return SimpleNamespace(stream_url=url, position=pos, duration=duration)


def test_engine_owns_single_parallel_tap(qapp, isolated_settings, monkeypatch):
    """One tap, every platform: the engine's only owned source is the
    parallel decode, fed directly to the FFT worker."""
    import jellytoast.visualizer as vis

    monkeypatch.setattr(vis, "get_now_playing", lambda: _fake_np())
    monkeypatch.setattr(vis.ParallelDecodeTap, "_ffmpeg_bin", staticmethod(lambda: None))
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
    monkeypatch.setattr(vis.ParallelDecodeTap, "_ffmpeg_bin", staticmethod(lambda: None))
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
    from jellytoast.visualizer import QtDecodeTap

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
    t.set_target_ms(90_000)  # a real seek, decode far behind
    _feed(t, _FFT_WINDOW)
    t()
    t()
    t()
    # queued connection → the GUI slot hasn't run, so the pending guard
    # must keep this to ONE emission across repeated worker ticks
    assert t._restart_pending is True
    qapp.processEvents()  # deliver the queued signal
    assert len(fired) == 1


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

    monkeypatch.delenv("JT_VIS_TAP", raising=False)
    monkeypatch.setattr(vis, "get_now_playing", lambda: _fake_np())
    monkeypatch.setattr(vis.QtDecodeTap, "_begin_decode", lambda self, ms: None)
    engine = vis.VisualizerEngine()
    try:
        assert isinstance(engine._parallel_tap, vis.QtDecodeTap)
        assert engine._tap is engine._parallel_tap
    finally:
        engine.stop(fast=True)


def test_engine_env_falls_back_to_ffmpeg_tap(qapp, isolated_settings, monkeypatch):
    import jellytoast.visualizer as vis

    monkeypatch.setenv("JT_VIS_TAP", "ffmpeg")
    monkeypatch.setattr(vis, "get_now_playing", lambda: _fake_np())
    engine = vis.VisualizerEngine()
    try:
        assert isinstance(engine._parallel_tap, vis.ParallelDecodeTap)
    finally:
        engine.stop(fast=True)
