"""Tests for ``jellytoast.visualizer`` — FFT math + signal-plumbing slice.

No rendering is exercised here (per ``docs/autonomous_tasks.md``,
visualizer rendering quality is the non-autonomous bit). These tests
cover:

- ``compute_bands`` math against synthetic sine / noise / silence.
- Silence pcm_callback → all-zero band emission.
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

from jellytoast.player_state import PlayerBus
from jellytoast.visualizer import (
    _BAND_COUNT,
    _FFT_WINDOW,
    MonitorAudioTap,
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
        from jellytoast.visualizer import _band_edges

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


# ── Silence source ──────────────────────────────────────────────────────────


class TestSilenceCallback:
    def test_callable_through_engine_yields_silence(self, fresh_bus, qapp):
        """Engine + a None-returning source → bus emits all-zero bands
        (a sourceless ParallelDecodeTap behaves the same way)."""
        engine = VisualizerEngine(pcm_callback=lambda: None)
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

    def test_immediate_stop_after_start_terminates_thread(self, fresh_bus, qapp):
        """stop() called immediately after start() — before the worker
        thread has begun executing run() — must still terminate the
        thread.

        Regression: run() unconditionally set ``_running=True`` on entry,
        clobbering an early ``stop()``'s ``_running=False``. The loop then
        spun forever, ``stop()``'s ``thread.wait()`` timed out, and the
        still-running QThread was ``deleteLater``'d — its destruction
        aborts the process (``QThread: Destroyed while thread is still
        running``) when a *later* test's event loop processes the pending
        delete. Surfaced under random test order; the ``_stop_requested``
        latch fixes it. Asserting ``isFinished`` here is the direct check
        — a leaked running thread would leave it False (and, pre-fix,
        abort a downstream test rather than fail this one)."""
        engine = VisualizerEngine(pcm_callback=lambda: None)
        engine.start()
        thread = engine._thread
        engine.stop()  # no sleep — race the worker thread's startup
        assert engine.is_running is False
        assert thread is not None
        assert not thread.isRunning()
        assert thread.isFinished()


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
        import jellytoast.visualizer as viz

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
        import jellytoast.visualizer as viz

        monkeypatch.setattr(
            viz.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None
        )
        cmd = MonitorAudioTap._build_capture_cmd(44100)
        assert cmd is not None
        assert cmd[0] == "pw-record"
        assert "stream.capture.sink=true" in cmd
        assert not any(arg.startswith("--target=") for arg in cmd)

    def test_falls_back_to_parec_without_pw_record(self, monkeypatch):
        import jellytoast.visualizer as viz

        monkeypatch.setattr(
            viz.shutil, "which", lambda name: "/usr/bin/parec" if name == "parec" else None
        )
        cmd = MonitorAudioTap._build_capture_cmd(44100)
        assert cmd is not None
        assert cmd[0] == "parec"
        assert f"--device={MonitorAudioTap.FALLBACK_SOURCE}" in cmd


# ── MonitorAudioTap — EOF reap + mid-session respawn (#12) ──────────────────


class _FakeProc:
    """Stand-in for a capture subprocess. ``stdout.read`` yields the
    queued chunks then EOF (b''); records wait()/close() so the reap is
    observable. Acts as its own ``stdout`` pipe."""

    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self.stdout = self
        self.waited = False
        self.closed = False

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def poll(self):
        return 0


class TestMonitorAudioTapRespawn:
    def test_eof_reaps_the_dead_process(self):
        # On EOF the tap must collect the zombie (wait) + close the pipe
        # FD — the old code just dropped the handle, leaking both.
        tap = MonitorAudioTap()
        fp = _FakeProc(chunks=[])  # read() → b'' immediately (EOF)
        tap._proc = fp
        assert tap() is None
        assert fp.waited is True
        assert fp.closed is True
        assert tap._proc is None

    def test_respawns_after_eof_once_backoff_elapses(self, monkeypatch):
        # A mid-session sink loss (EOF) must recover: the tap re-spawns on
        # a later cycle instead of staying permanently flat. Gated by the
        # backoff so it can't churn a process every window.
        import jellytoast.visualizer as viz

        monkeypatch.setattr(
            viz.shutil,
            "which",
            lambda name: "/usr/bin/pw-record" if name == "pw-record" else None,
        )
        spawned = []

        def _fake_popen(cmd, **kw):
            p = _FakeProc(chunks=[])  # each spawn immediately EOFs
            spawned.append(p)
            return p

        monkeypatch.setattr(viz.subprocess, "Popen", _fake_popen)

        clock = {"v": 0.0}
        tap = MonitorAudioTap(now_fn=lambda: clock["v"])
        tap.start()  # spawn #1
        assert len(spawned) == 1
        assert tap() is None  # reads EOF → reap → _proc=None
        assert tap._proc is None
        # Within the backoff window: no respawn.
        clock["v"] = tap._RESPAWN_BACKOFF_S - 0.1
        assert tap() is None
        assert len(spawned) == 1
        # Past the window: respawn fires.
        clock["v"] = tap._RESPAWN_BACKOFF_S + 0.1
        assert tap() is None
        assert len(spawned) == 2

    def test_no_respawn_storm_within_backoff(self, monkeypatch):
        import jellytoast.visualizer as viz

        monkeypatch.setattr(
            viz.shutil,
            "which",
            lambda name: "/usr/bin/pw-record" if name == "pw-record" else None,
        )
        spawned = []
        monkeypatch.setattr(
            viz.subprocess,
            "Popen",
            lambda cmd, **kw: (spawned.append(_FakeProc()), spawned[-1])[1],
        )
        tap = MonitorAudioTap(now_fn=lambda: 100.0)  # frozen clock
        tap.start()  # spawn #1, anchors the backoff at t=100
        for _ in range(5):
            tap()  # each hits EOF; frozen clock → never past the backoff
        assert len(spawned) == 1


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
    def test_worker_gates_emits_to_throttle_interval(self, fresh_bus, qapp, monkeypatch):
        """The worker must not emit faster than its ``emit_interval_s``
        throttle — it gates each tick on its monotonic clock.

        Drives the gate with an INJECTED fake clock (``now_fn``) and counts
        real ``bands_ready`` emits: zero wall-clock dependence, and it fails
        immediately if the ``elapsed < interval`` guard is removed.

        Why injected, not a global monkeypatch: patching
        ``jellytoast.visualizer.time.monotonic`` replaces the SHARED ``time``
        module process-wide, so a concurrent thread's ``time.monotonic()``
        (a leftover daemon, an async_io pool worker — more likely under
        CPU load or random test order) would advance the same fake clock
        and trip ``stop()`` early, collapsing the emit count to a handful
        and tripping the lower bound. That was the load-correlated flake.
        Injecting the clock into the worker means ONLY the worker advances
        it → deterministic regardless of any other thread.
        """
        interval = 0.033

        sink = []
        clock = {"t": 0.0}
        step = 0.005  # 5 ms of fake time per worker monotonic read
        max_fake_s = 2.0  # run over ~2 s of fake time
        worker_box = {}

        def fake_monotonic():
            # Read ONLY by the worker loop (injected below), so the count of
            # reads — and thus emits — is fixed by the gate logic, not by
            # wall time or other threads. Stops the worker once the window
            # is covered, so it terminates in a bounded number of iterations
            # whether the gate works or not (a removed gate trips the
            # ceiling assertion, never hangs).
            clock["t"] += step
            if clock["t"] >= max_fake_s and worker_box.get("w") is not None:
                worker_box["w"].stop()
            return clock["t"]

        worker = _FFTWorker(
            pcm_callback=lambda: np.zeros(_FFT_WINDOW, dtype=np.float32),
            emit_interval_s=interval,
            now_fn=fake_monotonic,  # injected — no global time patch
        )
        worker_box["w"] = worker
        worker.bands_ready.connect(lambda b: sink.append(b))

        # msleep no-op so the gated path doesn't actually sleep — keeps the
        # test instant. (Patches the module global, not the time module, so
        # it can't race the injected clock.)
        class _FakeQThread:
            @staticmethod
            def msleep(_ms):
                pass

        monkeypatch.setattr("jellytoast.visualizer.QThread", _FakeQThread)

        worker.run()  # runs synchronously on this thread until stop()

        # Over ~2 s of fake time at a 33 ms interval the worker emits
        # ~2.0/0.033 ≈ 60 times. A removed/broken gate would emit on nearly
        # every ~5 ms tick (~400) and trip the ceiling. The band is wide
        # enough to never flake yet still catch a busted gate.
        emitted = len(sink)
        max_allowed = (max_fake_s / interval) * 1.5 + 2  # generous ceiling
        assert emitted >= 5, f"worker barely emitted ({emitted})"
        assert emitted <= max_allowed, (
            f"worker emitted {emitted} times over {max_fake_s:.1f}s fake — "
            f"throttle ({interval * 1000:.0f}ms) not gating (ceiling {max_allowed:.0f})"
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
        from jellytoast.visualizer import _band_edges

        edges = _band_edges(44100, _BAND_COUNT)
        lo, hi = edges[peak_idx], edges[peak_idx + 1]
        assert lo <= 1000.0 <= hi
