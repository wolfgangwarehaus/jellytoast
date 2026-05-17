"""Tests for ``modules.visualizer`` — FFT math + signal-plumbing slice.

No rendering is exercised here (per ``docs/autonomous_tasks.md``,
visualizer rendering quality is the non-autonomous bit). These tests
cover:

- ``compute_bands`` math against synthetic sine / noise / silence.
- ``VisualizerEngine`` env-flag gating.
- ``MpvAudioTap`` stub returning silence.
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


@pytest.fixture
def no_viz_env(monkeypatch):
    """Ensure JT_VISUALIZER is unset for default-state tests."""
    monkeypatch.delenv("JT_VISUALIZER", raising=False)


@pytest.fixture
def viz_env(monkeypatch):
    """Force-enable the visualizer env flag."""
    monkeypatch.setenv("JT_VISUALIZER", "1")


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

    def test_callable_through_engine_yields_silence(self, fresh_bus, viz_env, qapp):
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


# ── VisualizerEngine env-flag gating ────────────────────────────────────────


class TestEngineGating:
    def test_start_is_noop_when_env_unset(self, fresh_bus, no_viz_env):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        assert engine.is_running is False
        # Stop is also safe to call.
        engine.stop()

    def test_start_constructs_thread_when_env_set(self, fresh_bus, viz_env, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        try:
            assert engine.is_running is True
            assert engine._thread is not None
            assert engine._worker is not None
        finally:
            engine.stop()

    def test_start_is_idempotent(self, fresh_bus, viz_env, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        first_thread = engine._thread
        engine.start()  # second call should be a no-op
        try:
            assert engine._thread is first_thread
        finally:
            engine.stop()

    def test_stop_is_idempotent(self, fresh_bus, viz_env, qapp):
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        engine.stop()
        engine.stop()  # no error, no second teardown
        assert engine.is_running is False


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
    def test_emit_interval_at_least_30hz_floor(self, fresh_bus, viz_env, qapp):
        """When the source is always-ready, emits should still be ~33ms
        apart (i.e. no faster than ~30 Hz)."""

        # A tap that always returns silence — produces zeros instantly,
        # so the only thing throttling emits is the worker's own gate.
        def always_zero():
            return np.zeros(_FFT_WINDOW, dtype=np.float32)

        engine = VisualizerEngine(pcm_callback=always_zero)
        bus = PlayerBus.get()

        timestamps: List[float] = []
        bus.visualizer_bands_changed.connect(lambda _b: timestamps.append(time.monotonic()))

        engine.start()
        _spin(qapp, 0.3)  # ~9 frames at 30 Hz
        engine.stop()
        _spin(qapp, 0.05)

        # We should have several emits, but not e.g. 100 of them (which
        # would mean the throttle is busted).
        assert 3 <= len(timestamps) <= 20, f"unexpected emit count: {len(timestamps)}"

        # Inter-emit deltas (excluding the first which has no predecessor).
        deltas = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        # Allow a small under-shoot for scheduling jitter, but anything
        # below ~20ms means the throttle isn't working.
        for d in deltas:
            assert d >= 0.020, f"emit interval {d * 1000:.1f}ms is too tight"


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
