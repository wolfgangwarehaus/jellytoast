"""Tests for ``modules.visualizer`` — FFT math + signal-plumbing slice.

No rendering is exercised here (per ``docs/autonomous_tasks.md``,
visualizer rendering quality is the non-autonomous bit). These tests
cover:

- ``compute_bands`` math against synthetic sine / noise / silence.
- ``MpvAudioTap`` silence stub.
- ``MonitorAudioTap`` interface contract (no subprocess actually
  spawned — that path needs a live PulseAudio/PipeWire instance).
- ``VisualizerEngine`` start/stop idempotence.
- The ``PlayerBus.visualizer_bands_changed`` signal contract.
- The ~30 Hz emit throttle.
"""

from __future__ import annotations

import time
from typing import List

import numpy as np
import pytest

from modules.player_state import PlayerBus
from modules.visualizer import (
    _BAND_COUNT,
    _FFT_WINDOW,
    MonitorAudioTap,
    MpvAudioTap,
    VisualizerEngine,
    _FFTWorker,
    compute_bands,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _sine(
    freq_hz: float, sample_rate: int = 44100, n: int = _FFT_WINDOW, amplitude: float = 0.5
) -> np.ndarray:
    """Generate a mono float32 sine wave of `n` samples."""
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def _capture(signal) -> List:
    """Collect every emission of `signal` into a list. Returns the list."""
    out: List = []
    signal.connect(lambda *args: out.append(args))
    return out


@pytest.fixture
def fresh_bus():
    """Each test gets a fresh PlayerBus singleton — connections from one
    test must not bleed into the next."""
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None




# ── compute_bands ───────────────────────────────────────────────────────────


class TestComputeBands:
    def test_output_length_matches_band_count(self):
        out = compute_bands(_sine(1000.0), 44100, band_count=32)
        assert len(out) == 32

    def test_default_band_count_is_32(self):
        out = compute_bands(_sine(1000.0), 44100)
        assert len(out) == _BAND_COUNT == 32

    def test_silence_all_zero(self):
        zeros = np.zeros(_FFT_WINDOW, dtype=np.float32)
        out = compute_bands(zeros, 44100)
        assert all(v == 0.0 for v in out)

    def test_values_in_unit_range(self):
        # Use a near-clipping amplitude to ensure we don't blow past 1.0.
        loud = _sine(1000.0, amplitude=0.99)
        out = compute_bands(loud, 44100)
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_silence_values_in_unit_range(self):
        zeros = np.zeros(_FFT_WINDOW, dtype=np.float32)
        out = compute_bands(zeros, 44100)
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_1khz_sine_peaks_at_1khz_band(self):
        """A pure 1 kHz tone should put the loudest band on the band
        whose range covers 1 kHz."""
        out = compute_bands(_sine(1000.0, amplitude=0.5), 44100, band_count=32)
        peak_idx = int(np.argmax(out))

        # Reconstruct band edges to confirm the peak landed where we expect.
        from modules.visualizer import _band_edges

        edges = _band_edges(44100, 32)
        lo, hi = edges[peak_idx], edges[peak_idx + 1]
        assert lo <= 1000.0 <= hi, (
            f"peak band {peak_idx} = [{lo:.1f}, {hi:.1f}] Hz doesn't bracket 1000 Hz"
        )

    def test_white_noise_roughly_flat(self):
        """White noise has equal power per Hz — log-spaced bands should
        give a roughly flat (within ~2x) distribution across non-empty bands."""
        rng = np.random.default_rng(seed=42)
        noise = rng.standard_normal(_FFT_WINDOW).astype(np.float32) * 0.3
        out = compute_bands(noise, 44100, band_count=32)
        non_empty = [v for v in out if v > 0.05]
        # Need a reasonable sample to compare against
        assert len(non_empty) >= 16, "expected most bands to register on white noise"
        lo = min(non_empty)
        hi = max(non_empty)
        # Within 2x is generous but pins down the "flat-ish" claim. The
        # math says it should be tighter; using 2x leaves headroom for
        # the Hann window's spectral spread + small-sample variance.
        assert hi / lo < 2.0, f"noise distribution too uneven: {lo:.3f}..{hi:.3f}"

    def test_short_input_zero_pads(self):
        """Input shorter than the FFT window is padded with zeros and
        should still produce a valid band vector."""
        short = _sine(1000.0, n=512)
        out = compute_bands(short, 44100)
        assert len(out) == _BAND_COUNT
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_long_input_truncated(self):
        """Input longer than the FFT window is truncated to the head."""
        long = _sine(1000.0, n=_FFT_WINDOW * 3)
        out = compute_bands(long, 44100)
        assert len(out) == _BAND_COUNT
        assert all(0.0 <= v <= 1.0 for v in out)

    def test_zero_sample_rate_returns_zeros(self):
        out = compute_bands(_sine(1000.0), sample_rate=0)
        assert out == [0.0] * _BAND_COUNT

    def test_zero_band_count_returns_empty(self):
        out = compute_bands(_sine(1000.0), 44100, band_count=0)
        assert out == []


# ── MpvAudioTap stub ────────────────────────────────────────────────────────


class TestMpvAudioTap:
    def test_returns_none(self):
        tap = MpvAudioTap()
        tap.start()
        assert tap() is None
        tap.stop()

    def test_callable_through_engine_yields_silence(self, fresh_bus, qapp):
        """Engine + stub tap → bus emits all-zero bands."""
        engine = VisualizerEngine(pcm_callback=MpvAudioTap())
        bus = PlayerBus.get()
        emissions = _capture(bus.visualizer_bands_changed)
        engine.start()

        # Pump events for ~150ms so the worker has time to fire ~3-4 frames.
        _spin(qapp, 0.15)
        engine.stop()
        _spin(qapp, 0.05)

        assert len(emissions) > 0, "stub tap + engine should still emit frames"
        for args in emissions:
            bands = args[0]
            assert bands == [0.0] * _BAND_COUNT


# ── VisualizerEngine lifecycle ──────────────────────────────────────────────


class TestEngineLifecycle:
    def test_start_constructs_thread(self, fresh_bus, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        try:
            assert engine.is_running is True
            assert engine._thread is not None
            assert engine._worker is not None
        finally:
            engine.stop()

    def test_start_is_idempotent(self, fresh_bus, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        first_thread = engine._thread
        engine.start()  # second call should be a no-op
        try:
            assert engine._thread is first_thread
        finally:
            engine.stop()

    def test_stop_is_idempotent(self, fresh_bus, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        engine.stop()
        engine.stop()  # no error, no second teardown
        assert engine.is_running is False


# ── MonitorAudioTap ─────────────────────────────────────────────────────────


class TestMonitorAudioTap:
    def test_returns_none_before_start(self):
        # No subprocess yet — must report no data without crashing.
        tap = MonitorAudioTap()
        assert tap() is None

    def test_stop_without_start_is_safe(self):
        tap = MonitorAudioTap()
        tap.stop()
        tap.stop()  # idempotent

    def test_missing_capture_tools_leave_tap_inert(self, monkeypatch):
        # When neither pw-record nor parec is installed, ``start`` should
        # log and leave the tap in the "no data" state rather than raising.
        import modules.visualizer as viz

        monkeypatch.setattr(viz.shutil, "which", lambda _name: None)
        tap = MonitorAudioTap()
        tap.start()
        assert tap._proc is None
        assert tap() is None

    def test_prefers_pw_record_when_available(self, monkeypatch):
        # ``pw-record`` captures the default sink's monitor via the
        # ``stream.capture.sink`` property — it must NOT target mpv's
        # stream node, which suppresses mpv's own link to the sink on
        # PipeWire 1.6.5+. Verify the command-builder prefers pw-record
        # when present and uses the sink-monitor capture property.
        import modules.visualizer as viz

        monkeypatch.setattr(
            viz.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None
        )
        cmd = MonitorAudioTap._build_capture_cmd(44100)
        assert cmd is not None
        assert cmd[0] == "pw-record"
        assert "stream.capture.sink=true" in cmd
        assert not any(arg.startswith("--target=") for arg in cmd)

    def test_falls_back_to_parec_without_pw_record(self, monkeypatch):
        import modules.visualizer as viz

        monkeypatch.setattr(
            viz.shutil, "which", lambda name: "/usr/bin/parec" if name == "parec" else None
        )
        cmd = MonitorAudioTap._build_capture_cmd(44100)
        assert cmd is not None
        assert cmd[0] == "parec"
        assert f"--device={MonitorAudioTap.FALLBACK_SOURCE}" in cmd


# ── PlayerBus signal contract ───────────────────────────────────────────────


class TestPlayerBusSignal:
    def test_visualizer_bands_changed_exists_and_accepts_list(self, fresh_bus):
        bus = PlayerBus.get()
        captured = _capture(bus.visualizer_bands_changed)
        sample = [0.1, 0.2, 0.3]
        bus.visualizer_bands_changed.emit(sample)
        assert captured == [(sample,)]


# ── Throttle to ~30 Hz ──────────────────────────────────────────────────────


def _spin(qapp, seconds: float) -> None:
    """Pump the Qt event loop for `seconds` seconds.

    Used to give the worker thread time to fire signals + the queued
    cross-thread connections time to deliver them on the GUI thread.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


class TestThrottle:
    def test_emit_rate_respects_30hz_throttle(self, fresh_bus, qapp):
        """When the source is always-ready, the emit RATE must respect the
        worker's ~30 Hz throttle ceiling.

        We assert on count-over-a-wall-clock-window, NOT on inter-emit
        deltas. The throttle lives in the worker thread, which gates on
        ``time.monotonic()`` (a hard ceiling no amount of CPU pressure can
        breach). But the timestamps we observe here are GUI-thread
        *delivery* times of the queued cross-thread signal — and on a
        starved/shared CI event loop (especially under ``pytest -n auto``)
        Qt coalesces the queued emits and delivers several back-to-back
        once the GUI thread finally runs. So individual (and even average)
        inter-delivery deltas can look arbitrarily tight while the worker
        is throttling perfectly. The total count over a fixed wall-clock
        window is immune to that coalescing: a queued connection preserves
        every emit, and the worker can't have produced them faster than
        the monotonic gate allows.
        """

        # A tap that always returns silence — produces zeros instantly,
        # so the only thing throttling emits is the worker's own gate.
        def always_zero():
            return np.zeros(_FFT_WINDOW, dtype=np.float32)

        engine = VisualizerEngine(pcm_callback=always_zero)
        bus = PlayerBus.get()

        count = 0

        def _tally(_b):
            nonlocal count
            count += 1

        bus.visualizer_bands_changed.connect(_tally)

        window_s = 0.3  # ~9 frames at 30 Hz
        t0 = time.monotonic()
        engine.start()
        _spin(qapp, window_s)
        engine.stop()
        _spin(qapp, 0.05)  # drain any queued emits
        elapsed = time.monotonic() - t0

        # Sanity floor: the worker actually ran and emitted (a generous
        # lower bound — extreme starvation could legitimately yield few).
        assert count >= 3, f"too few emits ({count}) — worker barely ran?"

        # Throttle ceiling: emit rate must not exceed ~30 Hz by more than a
        # tolerance. 1.5x leaves headroom for a boundary emit + window
        # measurement slop while still catching a throttle set materially
        # too fast (a busted gate free-runs at ~2x via the half-interval
        # yield sleep, which trips this).
        rate_hz = count / elapsed
        ceiling_hz = 1.0 / _EMIT_INTERVAL_S
        assert rate_hz <= ceiling_hz * 1.5, (
            f"emit rate {rate_hz:.1f}Hz exceeds throttle ceiling "
            f"{ceiling_hz:.1f}Hz (count={count}, window={elapsed * 1000:.0f}ms)"
        )


# ── Worker math integration (no env flag needed for the math path) ──────────


class TestWorkerMath:
    def test_worker_emits_bands_for_known_signal(self, fresh_bus, qapp):
        """Construct the worker directly (no engine, no env flag) and
        drive a single iteration to confirm bands_ready fires with a
        valid 32-band list."""
        samples = _sine(1000.0, amplitude=0.5)
        # One-shot callback: returns the signal once then None.
        calls = {"n": 0}

        def once():
            calls["n"] += 1
            return samples if calls["n"] == 1 else None

        worker = _FFTWorker(pcm_callback=once, emit_interval_s=0.001)
        out: List[List[float]] = []
        worker.bands_ready.connect(lambda b: out.append(b))

        # Run the loop briefly on the main thread, then stop.
        from threading import Thread

        def runner():
            worker.run()

        # We need a separate native thread because the worker's `run`
        # blocks. (For the production path it lives on a QThread; here
        # we keep it minimal.)
        t = Thread(target=runner, daemon=True)
        t.start()
        time.sleep(0.1)
        worker.stop()
        t.join(timeout=1.0)

        # Pump the GUI loop so DirectConnection-style emits land.
        qapp.processEvents()

        assert len(out) >= 1
        first = out[0]
        assert len(first) == _BAND_COUNT
        assert all(0.0 <= v <= 1.0 for v in first)
        # First emission was driven by the 1 kHz sine — peak should fall
        # in the band that covers 1 kHz.
        peak_idx = int(np.argmax(first))
        from modules.visualizer import _band_edges

        edges = _band_edges(44100, _BAND_COUNT)
        lo, hi = edges[peak_idx], edges[peak_idx + 1]
        assert lo <= 1000.0 <= hi
