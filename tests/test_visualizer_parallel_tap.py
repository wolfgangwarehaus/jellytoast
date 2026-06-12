"""ParallelDecodeTap — the bit-perfect-safe visualizer source.

A second, analysis-only ffmpeg decode of the playing stream,
consumer-paced against mpv's position clock. Activates whenever
bit-perfect is on or a direct alsa/ device is pinned (the monitor tap
has nothing to read there — and worse, an open monitor capture pins
PipeWire's graph rate). docs/research/visualizer_bit_perfect_2026-06-11.md
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from jellytoast.visualizer import (  # noqa: E402
    _FFT_WINDOW,
    ParallelDecodeTap,
    _should_use_parallel,
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


def test_selection_logic():
    assert _should_use_parallel("alsa/front:CARD=A2,DEV=0", False) is True
    assert _should_use_parallel("auto", True) is True
    assert _should_use_parallel("pipewire/sink1", True) is True
    assert _should_use_parallel("auto", False) is False
    assert _should_use_parallel("pipewire/sink1", False) is False


def test_engine_routes_to_parallel_when_bit_perfect(qapp, isolated_settings, monkeypatch):
    """Dual-tap routing: bit-perfect flips the active tap to the
    parallel decode and STOPS the monitor capture (an open monitor
    pins PipeWire's graph rate — the cava problem)."""
    from jellytoast.player_state import PlayerBus
    from jellytoast.visualizer import VisualizerEngine

    isolated_settings.audio_output_device = "auto"
    bus = PlayerBus.get()
    engine = VisualizerEngine()
    if engine._monitor_tap is None:
        pytest.skip("dual-tap mode is Linux-only")
    calls = []
    monkeypatch.setattr(engine._monitor_tap, "stop", lambda **k: calls.append("mon-stop"))
    monkeypatch.setattr(engine._monitor_tap, "start", lambda: calls.append("mon-start"))
    monkeypatch.setattr(engine._parallel_tap, "start", lambda: calls.append("par-start"))
    monkeypatch.setattr(
        engine._parallel_tap, "stop", lambda **k: calls.append("par-stop")
    )

    old_bp = getattr(bus, "bit_perfect_active", False)
    try:
        bus.bit_perfect_active = True
        engine._reselect_tap()
        assert engine._active_tap is engine._parallel_tap
        assert "mon-stop" in calls and "par-start" in calls

        bus.bit_perfect_active = False
        engine._reselect_tap()
        assert engine._active_tap is engine._monitor_tap
        assert "par-stop" in calls
    finally:
        bus.bit_perfect_active = old_bp
        engine.stop(fast=True)


def _fake_np(url="http://srv/stream.flac?x=1", pos=42_000, duration=180_000):
    from types import SimpleNamespace

    return SimpleNamespace(stream_url=url, position=pos, duration=duration)


def test_engine_seeds_inflight_track_on_construction(
    qapp, isolated_settings, monkeypatch
):
    """Engine is lazy-built mid-track (visualizer opened during
    playback): it must seed source state from the live session, or the
    parallel tap sits sourceless until the next track change — the
    flat-bars-under-bit-perfect bug."""
    import jellytoast.visualizer as vis
    from jellytoast.player_state import PlayerBus

    fake = _fake_np()
    monkeypatch.setattr(vis, "get_now_playing", lambda: fake)
    monkeypatch.setattr(vis.ParallelDecodeTap, "_ffmpeg_bin", staticmethod(lambda: None))
    isolated_settings.audio_output_device = "auto"
    bus = PlayerBus.get()
    old_bp = getattr(bus, "bit_perfect_active", False)
    engine = None
    try:
        bus.bit_perfect_active = True
        engine = vis.VisualizerEngine()
        assert engine._last_np is fake
        assert engine._last_pos_ms == 42_000
        if engine._monitor_tap is not None:  # Linux dual-tap mode
            engine._reselect_tap()  # what start() runs first
            assert engine._active_tap is engine._parallel_tap
            assert engine._parallel_tap._source == fake.stream_url
            assert engine._parallel_tap._target_ms == 42_000
    finally:
        bus.bit_perfect_active = old_bp
        if engine is not None:
            engine.stop(fast=True)


def test_engine_parallel_only_mode_off_linux(qapp, isolated_settings, monkeypatch):
    """Off Linux the engine owns ONLY the parallel decode tap — it
    serves all playback (not just bit-perfect) and start() feeds it the
    in-flight track."""
    import jellytoast.visualizer as vis

    fake = _fake_np()
    monkeypatch.setattr(vis, "IS_LINUX", False)
    monkeypatch.setattr(vis, "get_now_playing", lambda: fake)
    monkeypatch.setattr(vis.ParallelDecodeTap, "_ffmpeg_bin", staticmethod(lambda: None))
    engine = vis.VisualizerEngine()
    try:
        assert engine._monitor_tap is None
        assert engine._active_tap is engine._parallel_tap
        assert engine._owned_tap is engine._parallel_tap
        engine.start()
        assert engine._parallel_tap._source == fake.stream_url
    finally:
        engine.stop(fast=True)


def test_engine_restarts_monitor_on_device_change(
    qapp, isolated_settings, monkeypatch
):
    """Switching the pinned output sink mid-session must respawn an
    active monitor capture — its target sink is computed at spawn
    time, so without a bounce it keeps reading the old sink."""
    from jellytoast.visualizer import VisualizerEngine

    isolated_settings.audio_output_device = "auto"
    engine = VisualizerEngine()
    if engine._monitor_tap is None:
        pytest.skip("dual-tap mode is Linux-only")
    calls = []
    monkeypatch.setattr(engine._monitor_tap, "stop", lambda **k: calls.append("stop"))
    monkeypatch.setattr(engine._monitor_tap, "start", lambda: calls.append("start"))
    engine._started = True
    engine._active_tap = engine._monitor_tap
    try:
        engine._on_vis_device_changed("pipewire/other_sink")
        assert calls[-2:] == ["stop", "start"]
    finally:
        engine.stop(fast=True)


def test_engine_dispatch_returns_active_tap_output(qapp, isolated_settings):
    from jellytoast.visualizer import VisualizerEngine

    engine = VisualizerEngine()
    if engine._monitor_tap is None:
        pytest.skip("dual-tap mode is Linux-only")
    try:
        engine._active_tap = lambda: "sentinel"
        assert engine._pcm_dispatch() == "sentinel"
        engine._active_tap = None
        assert engine._pcm_dispatch() is None
    finally:
        engine.stop(fast=True)
