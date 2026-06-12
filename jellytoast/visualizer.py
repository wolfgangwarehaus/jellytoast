"""Audio visualizer FFT backend — signal plumbing, math, no rendering.

This module ships the backend slice of the visualizer per
``docs/research/visualizers.md`` — FFT math, the audio-tap contract, a
worker ``QThread``, and a ``PlayerBus.visualizer_bands_changed`` emit
path. Rendering lives in ``jellytoast/visualizer_widget.py``.

Audio source: the engine takes a pluggable ``pcm_callback`` returning
mono float32 samples.

  • ``MonitorAudioTap`` — the working Linux tap (default on Linux).
    Reads mono float32 PCM from the system's default audio output
    monitor source via a ``parec`` subprocess. Works on PulseAudio
    and PipeWire-pulse-compat systems. Captures whatever's playing
    through the default sink (jellytoast + any other audio), which is
    the right v1 behaviour — system-audio reactivity is what users
    expect from "the visualizer".
  • ``ParallelDecodeTap`` — bit-perfect / ALSA-direct tap on Linux,
    and the SOLE tap off Linux: an analysis-only ``ffmpeg`` decode of
    the same stream mpv plays, consumer-paced against the playback
    clock. OS-agnostic by construction. A monitor-style Windows
    backend (WASAPI loopback) / macOS (``CATapDescription`` 14.4+)
    remains the P4 upgrade for system-audio reactivity off Linux.

``numpy`` is required for FFT math (a bundled dependency). The soft-import
guard stays as defence: without numpy the engine logs a one-shot warning on
``start()`` and stays dormant; importing this module never touches numpy.

Threading: a dedicated ``QThread`` runs the ``_FFTWorker`` loop, which
pulls samples → FFT → mel-spaced bands and emits a ``bands_ready``
signal back to the engine on the GUI thread. Throttled to ~30 Hz
(33 ms minimum between emits) so the bus doesn't spam consumers.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from jellytoast.platform_compat import IS_LINUX
from jellytoast.player_state import PlayerBus, get_now_playing

if TYPE_CHECKING:
    import numpy as np

    NDArray = np.ndarray
else:
    NDArray = Any

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Number of log-spaced mel bands. 32 is the spec'd default in
# ``docs/research/visualizers.md`` §4 and §8 (``visualizer/spectrum/bands``
# can later be 16/32/64/128 at runtime).
_BAND_COUNT = 32

# Spectrum coverage: 50 Hz–16 kHz. Below 50 Hz is sub-bass that drives a
# single band and tends to dominate; above 16 kHz is mostly inaudible
# air. Matches the typical visualizer's "useful" range.
_FREQ_LOW_HZ = 50.0
_FREQ_HIGH_HZ = 16000.0

# FFT window size. 2048 samples at 44.1 kHz = ~46 ms of audio, which
# gives ~21 Hz frequency resolution — enough to resolve the lowest band
# edges and still respond in time for a 30 Hz GUI repaint.
_FFT_WINDOW = 2048

# Emit throttle. 33 ms ≈ 30 Hz: a comfortable visualizer cadence that
# leaves the GUI thread time for other repaints. The doc targets 60 Hz
# *draw* but only 60 Hz *FFT* if the audio source can keep up — for the
# pluggable callback path 30 Hz is a safer floor.
_EMIT_INTERVAL_S = 0.033

# Decibel floor for normalisation. Anything quieter than -80 dB maps to
# 0.0 in the output bands; 0 dB maps to 1.0.
_DB_FLOOR = -80.0
_DB_CEIL = 0.0


# ── numpy availability check ────────────────────────────────────────────────


def _numpy_available() -> bool:
    """True iff numpy is importable. Logged once on first miss so the
    engine stays dormant cleanly instead of crashing on first sample."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        if not getattr(_numpy_available, "_warned", False):
            logger.warning(
                "numpy not importable (it's a bundled dependency — a broken "
                "install?). The visualizer will stay dormant."
            )
            _numpy_available._warned = True  # type: ignore[attr-defined]
        return False
    return True


# ── FFT math (pure, dependency-free except numpy) ───────────────────────────


def _hann_window(n: int) -> NDArray:
    """Standard Hann window — reduces spectral leakage. No tuning."""
    import numpy as np

    return np.hanning(n).astype(np.float32)


def _band_edges(sample_rate: int, band_count: int) -> NDArray:
    """Log-spaced band edges in Hz, inclusive of low and high.

    Returns an array of length ``band_count + 1`` so each band is the
    half-open interval ``[edges[i], edges[i+1])``.

    The high edge is clamped to the Nyquist frequency (``sample_rate /
    2``) so we never address FFT bins that don't exist for low sample
    rates (e.g. 22050 Hz → Nyquist 11025 Hz < 16 kHz default cap).
    """
    import numpy as np

    high = min(_FREQ_HIGH_HZ, sample_rate / 2.0)
    return np.geomspace(_FREQ_LOW_HZ, high, band_count + 1)


def compute_bands(
    pcm_samples: NDArray,
    sample_rate: int,
    band_count: int = _BAND_COUNT,
) -> List[float]:
    """Compute a log-spaced mel-style band spectrum from mono PCM.

    Input: ``pcm_samples`` — 1-D float32 array of length ``_FFT_WINDOW``
    (shorter arrays are zero-padded; longer arrays are truncated to the
    head). ``sample_rate`` is the source rate in Hz.

    Output: a list of ``band_count`` floats, each clipped to [0.0, 1.0].
    Magnitude is dB-scaled with a -80 dB floor mapped to 0 and 0 dB
    mapped to 1, so a saturated band reads ~1.0 and silence reads ~0.0.
    """
    import numpy as np

    if band_count <= 0:
        return []
    if sample_rate <= 0:
        return [0.0] * band_count

    # Coerce input to fixed-length float32. Pad with zeros if short;
    # truncate if long. Either way the FFT runs on a known window so
    # bin centers are deterministic across calls.
    samples = np.asarray(pcm_samples, dtype=np.float32).ravel()
    if samples.size < _FFT_WINDOW:
        padded = np.zeros(_FFT_WINDOW, dtype=np.float32)
        padded[: samples.size] = samples
        samples = padded
    elif samples.size > _FFT_WINDOW:
        samples = samples[:_FFT_WINDOW]

    # Pure-silence fast path: skip the FFT entirely so we don't generate
    # log(0) warnings, and so the output is exactly zero (not
    # floating-point fuzz near zero).
    if not np.any(samples):
        return [0.0] * band_count

    # Hann window → rFFT → magnitude. rFFT only returns the positive
    # half of the spectrum (length N/2 + 1); each bin spans
    # sample_rate / N Hz.
    windowed = samples * _hann_window(_FFT_WINDOW)
    spectrum = np.abs(np.fft.rfft(windowed))

    # Frequency for each FFT bin. ``rfftfreq`` matches ``rfft``'s
    # output length exactly.
    bin_freqs = np.fft.rfftfreq(_FFT_WINDOW, d=1.0 / sample_rate)

    edges = _band_edges(sample_rate, band_count)
    bands = np.zeros(band_count, dtype=np.float32)
    for i in range(band_count):
        lo, hi = edges[i], edges[i + 1]
        # Half-open [lo, hi) — last band uses closed top so the highest
        # bin doesn't get dropped on the boundary.
        if i == band_count - 1:
            mask = (bin_freqs >= lo) & (bin_freqs <= hi)
        else:
            mask = (bin_freqs >= lo) & (bin_freqs < hi)
        if np.any(mask):
            # Mean magnitude across bins in this band. Mean (not max,
            # not sum) gives a stable read regardless of how many bins
            # fall in each band — log-spaced bands have wildly different
            # bin counts.
            bands[i] = float(spectrum[mask].mean())
        # Empty band stays 0.0 (typical for the lowest 1-2 bands when
        # FFT bin resolution is coarser than the band width).

    # Normalise by the FFT window so amplitude is independent of window
    # size, then convert to dB. Small epsilon avoids log10(0) — at the
    # dB floor it maps to 0.0 in the output anyway.
    normalised = bands / float(_FFT_WINDOW)
    db = 20.0 * np.log10(normalised + 1e-12)

    # Map [_DB_FLOOR, _DB_CEIL] → [0.0, 1.0], clip overflow.
    scaled = (db - _DB_FLOOR) / (_DB_CEIL - _DB_FLOOR)
    clipped = np.clip(scaled, 0.0, 1.0)
    return [float(v) for v in clipped]


# ── Audio-tap contract ──────────────────────────────────────────────────────

PcmCallback = Callable[[], Optional[NDArray]]


# NOTE — do not revive the retired ``MpvAudioTap`` plan (an ``af-add``
# filter shipping PCM out of libmpv): it's a dead end — libmpv has no
# audio-frame egress, and any inserted filter sits in the playback chain
# (format conversion → breaks the bit-perfect contract). See
# docs/research/visualizer_bit_perfect_2026-06-11.md §2. The portable
# path IS ``ParallelDecodeTap`` (now the sole tap off Linux); Windows
# shared-mode loopback (WASAPI loopback) remains the P4 option for a
# monitor-style backend.


class MonitorAudioTap:
    """Linux audio tap — captures the default sink's monitor.

    Conforms to the ``PcmCallback`` shape: ``__call__`` returns one
    ``_FFT_WINDOW``-sized chunk of mono float32 PCM, or ``None`` if the
    subprocess hasn't been started (or has died).

    Two capture strategies, picked in order:

      1. ``pw-record -P stream.capture.sink=true`` — PipeWire native
         capture of the *default sink's monitor*. The ``stream.capture
         .sink`` property tells WirePlumber to route this capture
         stream to the monitor of whatever sink is currently default,
         and to follow it when the default changes (Speakers ↔ the
         Sunshine virtual sinks).
      2. ``parec --device=@DEFAULT_MONITOR@`` — PulseAudio monitor of
         whatever sink is active. Used when pw-record is missing (pure
         PulseAudio system, or PipeWire shipped without its CLI).

    Both strategies produce raw little-endian float32 PCM at the
    configured sample rate, single channel, no header — we slice into
    FFT-window chunks inside ``__call__``.

    Why the sink monitor and not mpv's stream node: an earlier build
    ran ``pw-record --target=jellytoast`` to capture mpv's output node
    directly, so the bars only reacted to jellytoast's own audio. But
    PipeWire 1.6.5 changed link policy — a capture stream targeting a
    playback stream node *suppresses that node's link to the sink*, so
    mpv's audio reached only the tap and never the speakers (silent
    playback + flat bars). Capturing the sink monitor instead leaves
    mpv's routing completely untouched; the only cost is the visualizer
    now reacts to all audio on the sink, not just jellytoast's. Monitor
    capture is also the oldest, most stable path in the PipeWire/Pulse
    stack — it won't break on the next update.

    Why not mpv's ``--lavfi-complex`` (the doc's original "approach A"):
    getting PCM samples out of mpv's filter graph into Python requires
    a Lua script + libmpv socket pipe round-trip; OS-loopback ships
    today and matches the P4 cross-platform plan in
    ``architecture_cross_platform.md`` (the backend-package pattern
    used by ``autostart/`` / ``media_controls/`` / ``keep_above/``).
    """

    # PulseAudio fallback source — the default sink's monitor.
    FALLBACK_SOURCE = "@DEFAULT_MONITOR@"

    # Minimum gap between (re)spawn attempts. After the capture process
    # dies (sink switched/closed mid-session) __call__ re-spawns the tap,
    # but rate-limited so a permanently-gone sink doesn't spin up a new
    # process every FFT window.
    _RESPAWN_BACKOFF_S = 2.0

    def __init__(
        self,
        sample_rate: int = 44100,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._proc: Optional[subprocess.Popen] = None
        # Carry-over buffer for partial reads — the recorder emits
        # chunks at its own cadence (~latency_msec) which won't align
        # with our FFT window, so we accumulate until we have a full
        # window.
        self._buffer = bytearray()
        # Injectable clock (tests pin it) gating the respawn backoff.
        self._now: Callable[[], float] = now_fn if now_fn is not None else time.monotonic
        self._last_spawn_s: float = 0.0
        # Only re-spawn a tap that was actually started once (and whose
        # capture process later died); a never-started tap stays inert and
        # returns None, so __call__ never auto-starts behind start()'s back.
        self._ever_started: bool = False

    @staticmethod
    def _pinned_sink_node() -> Optional[str]:
        """The PipeWire/Pulse node name mpv is pinned to via the
        audio_output_device setting, or None for auto / ALSA-direct
        (→ follow the default sink). Without this the tap reads the
        DEFAULT sink's monitor while mpv plays into the pinned one —
        silence, flat bars (live find 2026-06-12: a Sunshine virtual
        sink held the default while music played on a pinned DAC)."""
        from jellytoast.settings import get_settings

        dev = (get_settings().audio_output_device or "auto").strip()
        for prefix in ("pipewire/", "pulse/"):
            if dev.startswith(prefix):
                return dev[len(prefix):]
        return None

    @classmethod
    def _build_capture_cmd(
        cls, sample_rate: int, target: Optional[str] = None
    ) -> Optional[list[str]]:
        """Pick the best available capture command. ``target`` is the
        sink node to monitor (None → default sink). Returns ``None`` if
        neither pw-record nor parec is on PATH (engine then stays inert
        and the widget shows the pre-signal caption)."""
        if shutil.which("pw-record") is not None:
            cmd = [
                "pw-record",
                # Capture a sink's monitor (the default's, following it
                # when it changes, unless a target pins one) instead of
                # targeting mpv's node — see the class docstring for
                # the PipeWire 1.6.5 reason.
                "-P",
                "stream.capture.sink=true",
                "--format=f32",
                "--channels=1",
                f"--rate={sample_rate}",
                "--latency=20ms",
            ]
            if target:
                cmd += ["--target", target]
            return cmd + ["-"]
        if shutil.which("parec") is not None:
            source = f"{target}.monitor" if target else cls.FALLBACK_SOURCE
            return [
                "parec",
                f"--device={source}",
                "--format=float32le",
                "--channels=1",
                f"--rate={sample_rate}",
                "--latency-msec=20",
                "--client-name=jellytoast-visualizer",
            ]
        return None

    def start(self) -> None:
        """Spawn the audio-capture subprocess. Idempotent; safe if the
        host has neither pw-record nor parec (logs once, leaves the
        tap inert)."""
        if self._proc is not None:
            return
        # Anchor the respawn backoff on every spawn attempt (success or
        # fail) so a missing sink doesn't get retried every FFT window.
        self._last_spawn_s = self._now()
        self._ever_started = True
        cmd = self._build_capture_cmd(self._sample_rate, self._pinned_sink_node())
        if cmd is None:
            if not getattr(MonitorAudioTap, "_warned_missing", False):
                logger.warning(
                    "MonitorAudioTap: neither pw-record nor parec found "
                    "in PATH — install pipewire (preferred) or "
                    "pulseaudio-utils to enable the visualizer audio tap."
                )
                MonitorAudioTap._warned_missing = True  # type: ignore[attr-defined]
            return
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            logger.warning(
                "MonitorAudioTap: failed to spawn %s (%s)", cmd[0], exc
            )
            self._proc = None

    def stop(self, *, fast: bool = False) -> None:
        """Terminate the subprocess and drop the read buffer. Idempotent.

        ``fast=True`` skips ``proc.wait()`` and goes straight to
        ``proc.kill()`` without a wait — used on app shutdown where any
        delay is user-visible and the OS will reap the orphaned process
        as the process group dies anyway."""
        proc, self._proc = self._proc, None
        self._buffer = bytearray()
        if proc is None:
            return
        try:
            if fast:
                proc.kill()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
        except OSError:
            pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

    def _reap_dead(self, proc: "subprocess.Popen") -> None:
        """Best-effort reap of a capture process that hit EOF (already
        exited): collect the zombie via ``wait`` and close the stdout
        pipe FD. Without this the EOF path leaked an FD + left a zombie
        until interpreter exit."""
        if proc is None:
            return
        try:
            proc.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def __call__(self) -> Optional[NDArray]:
        """Read one FFT-window of mono float32 samples from the pipe.

        Blocks for up to one audio-buffer worth (~20-50 ms per parec
        chunk) — the FFT worker thread loop is OK with that, the
        ~21 Hz natural data rate (2048 samples / 44.1 kHz) sits below
        the worker's 30 Hz throttle ceiling.
        """
        if self._proc is None:
            # No capture process. If the tap was started and its process
            # later died (sink switched/closed mid-session), try to
            # re-spawn — rate-limited by _RESPAWN_BACKOFF_S so a sink
            # that's gone for good doesn't churn a process every window —
            # so the visualizer recovers instead of staying permanently
            # flat. A never-started tap stays inert (returns None) and is
            # not auto-started here.
            if (
                self._ever_started
                and self._now() - self._last_spawn_s >= self._RESPAWN_BACKOFF_S
            ):
                self.start()
            if self._proc is None or self._proc.stdout is None:
                return None
        elif self._proc.stdout is None:
            return None
        target_bytes = _FFT_WINDOW * 4  # 4 bytes per float32 sample
        try:
            while len(self._buffer) < target_bytes:
                chunk = self._proc.stdout.read(target_bytes - len(self._buffer))
                if not chunk:
                    # EOF — the capture process died or was stopped. Reap
                    # it (collect the zombie + close the pipe FD) and clear
                    # the handle; a later __call__ re-spawns the tap (the
                    # backoff above prevents a respawn storm).
                    self._reap_dead(self._proc)
                    self._proc = None
                    return None
                self._buffer.extend(chunk)
        except (OSError, ValueError):
            return None
        data = bytes(self._buffer[:target_bytes])
        del self._buffer[:target_bytes]
        import numpy as np

        return np.frombuffer(data, dtype=np.float32)


class ParallelDecodeTap:
    """Bit-perfect-safe audio tap — a second, analysis-only decode.

    ``ffmpeg -i <source> -f f32le …`` of the SAME stream mpv plays,
    consumer-paced against mpv's playback clock (``position_updated``).
    The playback chain is untouched — this is how foobar2000/Strawberry
    keep visualizations alive under exclusive output. Activates whenever
    the bit-perfect contract is live (the monitor tap both has nothing
    to read on ALSA-direct AND pins PipeWire's graph sample rate while
    open — docs/research/visualizer_bit_perfect_2026-06-11.md).

    Conforms to the ``PcmCallback`` shape. Sync model: track the decode
    stream's position (``anchor + consumed`` samples) against the
    playback target; read one window when roughly aligned, drop backlog
    when behind, return ``None`` when ahead (paused), and kill+respawn
    with ``-ss`` when a real seek opens a gap. ``live=True`` (internet
    radio — no timeline) skips sync entirely and just streams windows.

    The target clock EXTRAPOLATES between ``set_target_ms`` ticks while
    unpaused (capped, so a stalled feed can't run away): position
    updates arrive in discrete steps, and gating reads on the frozen
    last tick made the tap alternate read-bursts with ahead-of-clock
    ``None`` returns — the engine paints zeros for those, so the wave
    visibly jittered between live bands and baseline decay (the
    monitor tap's continuous pipe never has this problem). The engine
    pushes pause state via ``set_paused`` so extrapolation freezes
    while playback is actually paused.
    No ``-re``: kernel pipe back-pressure caps ffmpeg's read-ahead at
    ~0.4 s, and consumer-paced reads are drift-free by construction.
    """

    _RESPAWN_BACKOFF_S = 2.0
    # |lead| beyond this many seconds means a real seek — reseek ffmpeg.
    _RESTART_THRESHOLD_S = 2.0
    # Max seconds the target clock may extrapolate past the last
    # set_target_ms tick. Bounds drift when the position feed stalls
    # for reasons other than pause (buffering, track-end races).
    _EXTRAPOLATE_CAP_S = 2.0
    # Alignment slop, in FFT windows (~93 ms at 44.1 kHz).
    _SLOP_WINDOWS = 2
    # Max backlog windows dropped per __call__ — bounds the worst-case
    # block; the remainder catches up across the next ticks.
    _MAX_DISCARD_PER_TICK = 16

    def __init__(
        self,
        sample_rate: int = 44100,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._proc: Optional[subprocess.Popen] = None
        self._buffer = bytearray()
        self._now: Callable[[], float] = now_fn if now_fn is not None else time.monotonic
        self._last_spawn_s: float = 0.0
        self._started = False
        self._source = ""
        self._live = False
        self._target_ms: int = -1
        # Monotonic stamp of the last set_target_ms; 0.0 ⇒ never set,
        # extrapolation disabled (target treated as frozen).
        self._target_set_s: float = 0.0
        self._paused = False
        self._anchor_samples: int = 0
        self._consumed: int = 0
        # Most recent window served — re-served on ahead-while-playing
        # ticks (window 46 ms > tick 33 ms ⇒ ~1 in 3 ticks has no fresh
        # window; a None there paints ZERO bands and the wave flickers).
        self._last_window: Optional[NDArray] = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._started = True

    def stop(self, *, fast: bool = False) -> None:
        """Kill the decode subprocess (source survives — a later
        ``__call__`` respawns at the current target)."""
        proc, self._proc = self._proc, None
        self._buffer = bytearray()
        if proc is None:
            return
        try:
            if fast:
                proc.kill()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

    def clear(self) -> None:
        """Playback stopped — drop the source so the tap goes silent."""
        self.stop(fast=True)
        self._source = ""
        self._target_ms = -1
        self._target_set_s = 0.0
        self._last_window = None

    # ── engine-facing state pushes (GUI thread; reads on worker) ────

    def set_source(self, source: str, *, start_ms: int = 0, live: bool = False) -> None:
        """New track. ``source`` is mpv's stream URL (auth baked into
        the query string) or a ``file://`` local blob."""
        if source.startswith("file://"):
            from urllib.parse import unquote, urlparse

            source = unquote(urlparse(source).path)
        self._source = source
        self._live = bool(live)
        self._target_ms = int(start_ms)
        # Start the extrapolation clock NOW — waiting for the first
        # position_updated tick left the target frozen at start_ms for
        # ~a second of dead bars at every track start.
        self._target_set_s = self._now()
        self._last_window = None
        self.stop(fast=True)
        # spawn lazily on the next __call__ (worker thread) — keeps this
        # GUI-thread push cheap and the backoff bookkeeping in one place.

    def set_target_ms(self, ms: int) -> None:
        self._target_ms = int(ms)
        self._target_set_s = self._now()

    def set_paused(self, paused: bool) -> None:
        """Engine-pushed pause state. While paused the target clock
        freezes at the last tick; on resume it re-anchors so pause
        wall-time doesn't count as playback progress."""
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        if not paused and self._target_set_s > 0.0:
            self._target_set_s = self._now()

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _ffmpeg_bin() -> Optional[str]:
        return shutil.which("ffmpeg")

    def _build_cmd(self, start_s: float) -> Optional[list[str]]:
        bin_ = self._ffmpeg_bin()
        if bin_ is None:
            if not getattr(ParallelDecodeTap, "_warned_missing", False):
                logger.warning(
                    "ParallelDecodeTap: ffmpeg not found in PATH — the "
                    "visualizer can't tap bit-perfect/direct output "
                    "without it."
                )
                ParallelDecodeTap._warned_missing = True  # type: ignore[attr-defined]
            return None
        cmd = [bin_, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if start_s > 0.05 and not self._live:
            cmd += ["-ss", f"{start_s:.3f}"]
        cmd += [
            "-i", self._source,
            "-vn", "-map", "a:0",
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", "1", "-ar", str(self._sample_rate),
            "pipe:1",
        ]
        return cmd

    def _spawn(self, start_s: float) -> None:
        self._last_spawn_s = self._now()
        cmd = self._build_cmd(start_s)
        if cmd is None:
            return
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            logger.warning("ParallelDecodeTap: failed to spawn ffmpeg (%s)", exc)
            self._proc = None
            return
        self._buffer = bytearray()
        self._anchor_samples = int(start_s * self._sample_rate)
        self._consumed = 0

    def _read_window(self) -> Optional[bytes]:
        """One FFT window of raw bytes from the pipe, or None on EOF /
        error (EOF reaps the process; the respawn backoff recovers)."""
        if self._proc is None or self._proc.stdout is None:
            return None
        target_bytes = _FFT_WINDOW * 4
        try:
            while len(self._buffer) < target_bytes:
                chunk = self._proc.stdout.read(target_bytes - len(self._buffer))
                if not chunk:
                    proc = self._proc
                    self._proc = None
                    try:
                        proc.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass
                    return None
                self._buffer.extend(chunk)
        except (OSError, ValueError):
            return None
        data = bytes(self._buffer[:target_bytes])
        del self._buffer[:target_bytes]
        return data

    def __call__(self) -> Optional[NDArray]:
        if not self._started or not self._source:
            return None
        rate = self._sample_rate
        win = _FFT_WINDOW
        target = None
        if self._target_ms >= 0:
            t_ms = float(self._target_ms)
            if not self._paused and self._target_set_s > 0.0:
                # Position ticks are discrete; the playback clock isn't.
                # Extrapolate between ticks (capped) so reads pace
                # continuously instead of burst-then-zeros.
                t_ms += (
                    min(
                        self._now() - self._target_set_s,
                        self._EXTRAPOLATE_CAP_S,
                    )
                    * 1000.0
                )
            target = int(t_ms * rate / 1000)
        if self._proc is None:
            if self._now() - self._last_spawn_s < self._RESPAWN_BACKOFF_S:
                return None
            self._spawn((target / rate) if (target and not self._live) else 0.0)
            if self._proc is None:
                return None
        if not self._live and target is not None:
            have = self._anchor_samples + self._consumed
            lead = have - target
            slop = self._SLOP_WINDOWS * win
            if lead > slop:
                if self._paused:
                    # Paused: no read, no hold — the engine emits
                    # zeros and the wave decays to baseline.
                    return None
                # Playing but momentarily ahead (window-vs-tick
                # quantization): hold the last window so the wave
                # doesn't dip to zero between reads. The monitor tap
                # never has these gaps — it blocks on its pipe.
                return self._last_window
            if -lead > self._RESTART_THRESHOLD_S * rate:
                # Real seek — reseek the decoder.
                self.stop(fast=True)
                self._spawn(target / rate)
                if self._proc is None:
                    return None
            elif -lead > slop:
                # Behind: decode runs >> realtime, so drop backlog
                # (bounded per tick) until roughly aligned.
                deficit = min(
                    int(-lead // win) - 1, self._MAX_DISCARD_PER_TICK
                )
                for _ in range(max(0, deficit)):
                    if self._read_window() is None:
                        return None
                    self._consumed += win
        data = self._read_window()
        if data is None:
            return None
        self._consumed += win
        import numpy as np

        arr = np.frombuffer(data, dtype=np.float32)
        self._last_window = arr
        return arr


def _should_use_parallel(device: str, bit_perfect_active: bool) -> bool:
    """Tap selection: the parallel decode serves whenever the monitor
    tap would either find nothing (direct ALSA bypasses PipeWire) or
    do harm (an open monitor capture pins the graph sample rate, which
    silently defeats the bit-perfect rate-following config)."""
    return (device or "").startswith("alsa/") or bool(bit_perfect_active)


# ── FFT worker (runs on a dedicated QThread) ────────────────────────────────


class _FFTWorker(QObject):
    """Owns the FFT loop. Lives on a dedicated ``QThread``.

    Pulls samples from ``pcm_callback`` on each tick, runs ``compute_bands``,
    and emits ``bands_ready`` back to the engine. The engine handles the
    cross-thread relay onto ``PlayerBus.visualizer_bands_changed``.

    Throttling lives here (in the worker loop) rather than at the bus
    boundary so we don't waste CPU on FFTs we'd just discard.
    """

    bands_ready = Signal(list)

    def __init__(
        self,
        pcm_callback: PcmCallback,
        sample_rate: int = 44100,
        band_count: int = _BAND_COUNT,
        emit_interval_s: float = _EMIT_INTERVAL_S,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__()
        self._pcm_callback = pcm_callback
        self._sample_rate = int(sample_rate)
        self._band_count = int(band_count)
        self._emit_interval_s = float(emit_interval_s)
        self._running = False
        # Latch set by stop(). Distinct from _running because run() sets
        # _running True on entry: if stop() arrives BEFORE the worker
        # thread has begun executing run() (an immediate start()/stop()),
        # that early _running=False would be clobbered by run()'s own
        # _running=True and the loop would spin forever — leaking a live
        # QThread that aborts the process when a later event loop deletes
        # it. _stop_requested is write-once-True and never reset, so run()
        # can honour an early stop no matter the start/stop interleaving.
        self._stop_requested = False
        self._last_emit_s = 0.0
        # Clock the throttle gate reads. Injectable so a test can drive it
        # deterministically WITHOUT monkeypatching the global ``time``
        # module (which is shared process-wide — a concurrent thread's
        # ``time.monotonic()`` would then race the test's fake clock).
        self._now: Callable[[], float] = now_fn if now_fn is not None else time.monotonic

    @Slot()
    def run(self) -> None:
        """Main loop. Runs until ``stop()`` flips ``_running`` to False.

        Yields between ticks via ``QThread.msleep`` (called via the
        thread the worker was moved onto) so the loop doesn't pin a
        core. The yield interval is half the emit interval, which keeps
        latency low without busy-spinning.
        """
        self._running = True
        # Honour a stop() that landed before this thread started running.
        # _stop_requested is set True last by stop() and never reset, so
        # this check catches every start/stop interleaving (see __init__).
        if self._stop_requested:
            self._running = False
            return
        sleep_ms = max(1, int(self._emit_interval_s * 1000 / 2))
        zeros: List[float] = [0.0] * self._band_count

        while self._running:
            now = self._now()
            elapsed = now - self._last_emit_s
            if elapsed < self._emit_interval_s:
                QThread.msleep(sleep_ms)
                continue

            samples = None
            try:
                samples = self._pcm_callback()
            except Exception:  # noqa: BLE001
                # A misbehaving tap shouldn't bring down the worker —
                # just emit silence and try again next tick.
                samples = None

            if samples is None:
                bands = zeros
            else:
                bands = compute_bands(samples, self._sample_rate, self._band_count)

            self._last_emit_s = now
            self.bands_ready.emit(bands)

    @Slot()
    def stop(self) -> None:
        """Request the loop exit. Safe to call from any thread, and safe
        to call before ``run()`` has started (the ``_stop_requested``
        latch is checked on entry). Set ``_stop_requested`` first so a
        racing ``run()`` that observes ``_running=True`` still sees the
        latch on its post-assignment check."""
        self._stop_requested = True
        self._running = False


# ── Engine ──────────────────────────────────────────────────────────────────


class VisualizerEngine(QObject):
    """Owns the audio tap + FFT thread; re-emits to PlayerBus.

    Construction is cheap and side-effect-free. ``start()`` is a no-op
    unless ``JT_VISUALIZER=1`` is set, so leaving the engine in main is
    safe at all times. ``start()`` / ``stop()`` are idempotent.

    Wire your audio source via the ``pcm_callback`` constructor arg —
    any zero-arg callable returning a float32 ndarray (or ``None``).
    Default (no callback): Linux owns a monitor tap + parallel-decode
    tap pair routed by ``_reselect_tap``; other OSes own the
    parallel-decode tap alone.
    """

    def __init__(
        self,
        pcm_callback: Optional[PcmCallback] = None,
        sample_rate: int = 44100,
        band_count: int = _BAND_COUNT,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._tap: PcmCallback
        self._owned_tap: Optional[Any]
        # Dual owned taps (Linux): the monitor capture for the normal
        # shared path, the parallel decode for bit-perfect / ALSA-direct.
        # The worker holds _pcm_dispatch, so swapping the active tap is
        # an attribute write — no thread surgery.
        self._monitor_tap: Optional[MonitorAudioTap] = None
        self._parallel_tap: Optional[ParallelDecodeTap] = None
        self._active_tap: Optional[PcmCallback] = None
        self._last_np: Optional[Any] = None
        self._last_pos_ms: int = 0
        if pcm_callback is None:
            if IS_LINUX:
                self._monitor_tap = MonitorAudioTap(sample_rate=sample_rate)
                self._parallel_tap = ParallelDecodeTap(sample_rate=sample_rate)
                self._active_tap = self._monitor_tap
                self._owned_tap = self._monitor_tap
            else:
                # Portable parallel-only mode (Windows / macOS): no
                # monitor capture exists off Linux, but the ffmpeg
                # parallel decode is OS-agnostic — it serves ALL
                # playback here, not just bit-perfect. Degrades to
                # silence (flat bars) when ffmpeg isn't on PATH.
                self._parallel_tap = ParallelDecodeTap(sample_rate=sample_rate)
                self._active_tap = self._parallel_tap
                self._owned_tap = self._parallel_tap
            self._tap = self._pcm_dispatch
        else:
            self._owned_tap = None
            self._tap = pcm_callback
        self._sample_rate = int(sample_rate)
        self._band_count = int(band_count)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_FFTWorker] = None
        self._started = False
        self._bus = PlayerBus.get()
        # Casting silences the local mpv pipeline — the MonitorAudioTap
        # (PipeWire default-sink monitor) would otherwise keep emitting
        # whatever the system mixer carries (other apps, system sounds),
        # producing a spectrum that doesn't reflect what's actually
        # playing on the cast receiver. Track the cast state so
        # _on_bands_ready can substitute a flat-zero vector while a
        # cast is live; the worker keeps running so playback-resume
        # snaps back instantly.
        self._cast_active = bool(self._bus.cast_active)
        self._bus.cast_started.connect(self._on_cast_started)
        self._bus.cast_stopped.connect(self._on_cast_stopped)
        # Tap routing inputs (only meaningful with the dual owned taps —
        # the slots no-op otherwise). position_updated is the parallel
        # tap's clock; playback_started carries the source URL.
        if self._parallel_tap is not None:
            self._bus.playback_started.connect(self._on_vis_playback_started)
            self._bus.position_updated.connect(self._on_vis_position)
            self._bus.playback_stopped.connect(self._on_vis_playback_over)
            self._bus.playback_ended.connect(self._on_vis_playback_over)
            # Pause state gates the tap's clock extrapolation — without
            # it the bars would keep dancing through a paused track.
            self._bus.playback_paused.connect(
                lambda: self._parallel_tap.set_paused(True)
            )
            self._bus.playback_resumed.connect(
                lambda: self._parallel_tap.set_paused(False)
            )
            self._bus.bit_perfect_active_changed.connect(
                lambda _on: self._reselect_tap()
            )
            self._bus.audio_output_device_changed.connect(
                self._on_vis_device_changed
            )
            # The engine is lazy-built the first time the visualizer
            # opens — usually mid-track, AFTER playback_started fired —
            # so seed the source state from the live session. Without
            # this the parallel tap sits sourceless (flat bars under
            # bit-perfect) until the next track change.
            np_now = get_now_playing()
            if np_now is not None and getattr(np_now, "stream_url", ""):
                self._last_np = np_now
                self._last_pos_ms = int(getattr(np_now, "position", 0) or 0)
                self._parallel_tap.set_paused(
                    bool(getattr(np_now, "is_paused", False))
                )

    # ── Tap routing (dual owned taps) ─────────────────────────────────

    def _pcm_dispatch(self) -> Optional[NDArray]:
        tap = self._active_tap
        return tap() if tap is not None else None

    def _on_vis_playback_started(self, np_obj) -> None:
        self._last_np = np_obj
        self._last_pos_ms = int(getattr(np_obj, "position", 0) or 0)
        if self._parallel_tap is not None:
            self._parallel_tap.set_paused(False)
        if self._active_tap is self._parallel_tap and self._parallel_tap:
            self._push_source(self._parallel_tap, np_obj, self._last_pos_ms)
        # Track changes can also flip bit_perfect_active (lossy ↔
        # lossless) — cheap to re-evaluate here.
        self._reselect_tap()

    def _on_vis_position(self, ms: int) -> None:
        self._last_pos_ms = int(ms)
        if self._parallel_tap is not None:
            self._parallel_tap.set_target_ms(ms)

    def _on_vis_device_changed(self, _dev: str) -> None:
        """A device move can change BOTH which tap serves and which sink
        the monitor tap must read (its capture target is computed at
        spawn). Re-route, then bounce an already-active monitor capture
        so it respawns against the new pinned sink."""
        self._reselect_tap()
        if (
            self._active_tap is self._monitor_tap
            and self._monitor_tap is not None
            and self._started
        ):
            self._monitor_tap.stop(fast=True)
            self._monitor_tap.start()

    def _on_vis_playback_over(self) -> None:
        self._last_np = None
        if self._parallel_tap is not None:
            self._parallel_tap.clear()

    @staticmethod
    def _push_source(tap: "ParallelDecodeTap", np_obj, pos_ms: int) -> None:
        url = getattr(np_obj, "stream_url", "") or ""
        if not url:
            tap.clear()
            return
        # No usable timeline (internet radio) → live mode: decode
        # unsynced; bars run near-real-time off the stream buffer.
        live = int(getattr(np_obj, "duration", 0) or 0) <= 0
        tap.set_source(url, start_ms=pos_ms, live=live)

    def _reselect_tap(self) -> None:
        """Re-evaluate which owned tap should serve, swap live, and keep
        the inactive one's subprocess DOWN (stopping the monitor while
        bit-perfect is on is itself a fix — an open monitor capture pins
        PipeWire's graph sample rate)."""
        if self._monitor_tap is None or self._parallel_tap is None:
            return
        from jellytoast.settings import get_settings

        use_parallel = _should_use_parallel(
            get_settings().audio_output_device or "auto",
            bool(getattr(self._bus, "bit_perfect_active", False)),
        )
        if use_parallel and self._active_tap is not self._parallel_tap:
            self._monitor_tap.stop()
            self._parallel_tap.start()
            if self._last_np is not None:
                self._push_source(self._parallel_tap, self._last_np, self._last_pos_ms)
            self._active_tap = self._parallel_tap
        elif not use_parallel and self._active_tap is not self._monitor_tap:
            self._parallel_tap.stop(fast=True)
            if self._started:
                self._monitor_tap.start()
            self._active_tap = self._monitor_tap

    @property
    def band_count(self) -> int:
        return self._band_count

    @property
    def is_running(self) -> bool:
        return self._started

    def start(self) -> None:
        """Spin up the FFT worker thread. No-op if numpy is unavailable."""
        if self._started:
            return
        if not _numpy_available():
            return

        if self._active_tap is not None:
            # Owned-tap mode: pick the right tap for the CURRENT state
            # (bit-perfect may already be live when the page opens),
            # then start only that one. Off Linux _reselect_tap no-ops
            # and the parallel tap is the permanent active tap.
            self._reselect_tap()
            tap = self._active_tap
            if tap is not None:
                tap_start = getattr(tap, "start", None)
                if tap_start is not None:
                    tap_start()
            # Feed the in-flight track to the parallel tap when it's
            # serving from the first frame (engine built mid-track on
            # Windows, or under already-active bit-perfect on Linux).
            # set_source pre-spawn is field writes — re-pushing what
            # _reselect_tap already pushed is harmless.
            if tap is self._parallel_tap and self._last_np is not None:
                self._push_source(self._parallel_tap, self._last_np, self._last_pos_ms)

        self._thread = QThread()
        self._worker = _FFTWorker(
            pcm_callback=self._tap,
            sample_rate=self._sample_rate,
            band_count=self._band_count,
        )
        self._worker.moveToThread(self._thread)
        # Worker emits bands on its own thread; QueuedConnection
        # marshals them onto the bus's (GUI) thread before fan-out.
        self._worker.bands_ready.connect(self._on_bands_ready)
        self._thread.started.connect(self._worker.run)
        self._thread.start()
        self._started = True

    def stop(self, *, fast: bool = False) -> None:
        """Tear down the FFT worker. Idempotent.

        Order matters: flag the worker to exit its loop *first*, then
        wait for ``QThread.wait`` to confirm ``run`` has returned, only
        then drop our Python references. Dropping a QThread Python
        ref while the underlying C++ thread is still running triggers a
        Qt fatal abort.

        ``fast=True`` shortens the QThread.wait to 100 ms and asks the
        owned tap to kill its subprocess without waiting — used during
        app shutdown so the user sees windows vanish promptly. The
        process is exiting anyway, so the slim chance the worker
        thread is mid-``read`` doesn't matter."""
        if not self._started:
            return
        # 1. Flag the worker so its loop exits on the next iteration.
        if self._worker is not None:
            self._worker.stop()
        # 2. Block until the worker's ``run`` method actually returns
        # (the thread emits ``finished`` and stops).
        thread = self._thread
        worker = self._worker
        # Clear our flags before deleteLater so a re-entrant ``start``
        # call observes the engine as stopped.
        self._thread = None
        self._worker = None
        self._started = False
        if thread is not None:
            thread.quit()
            thread.wait(100 if fast else 2000)
            # Schedule C++ cleanup on the Qt side. deleteLater runs on
            # the thread that owns the object — for the QThread itself
            # that's the GUI thread, which has the event loop.
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        if self._owned_tap is not None:
            tap_stop = getattr(self._owned_tap, "stop", None)
            if tap_stop is not None:
                try:
                    tap_stop(fast=fast)
                except TypeError:
                    # Tap implementations without the kwarg (the silence
                    # stub) — call positionally.
                    tap_stop()
        if self._parallel_tap is not None:
            self._parallel_tap.stop(fast=fast)

    @Slot(list)
    def _on_bands_ready(self, bands: List[float]) -> None:
        """Relay worker output onto the global bus. Substitute a
        flat-zero band vector while casting so the visualizer doesn't
        track ambient desktop audio (PipeWire monitor) instead of the
        track playing on the cast receiver."""
        if self._cast_active:
            bands = [0.0] * self._band_count
        self._bus.visualizer_bands_changed.emit(bands)

    @Slot()
    def _on_cast_started(self) -> None:
        self._cast_active = True
        # Push one flat frame immediately so the surface drains its
        # last live bars within a tick instead of holding them stale
        # until the next worker emit.
        self._bus.visualizer_bands_changed.emit([0.0] * self._band_count)

    @Slot()
    def _on_cast_stopped(self) -> None:
        self._cast_active = False
