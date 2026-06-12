"""Audio visualizer FFT backend — signal plumbing, math, no rendering.

This module ships the backend slice of the visualizer per
``docs/research/visualizers.md`` — FFT math, the audio-tap contract, a
worker ``QThread``, and a ``PlayerBus.visualizer_bands_changed`` emit
path. Rendering lives in ``jellytoast/visualizer_widget.py``.

Audio source: the engine takes a pluggable ``pcm_callback`` returning
mono float32 samples. The default owned source is ``QtDecodeTap`` — an
in-process QtMultimedia decode of the same stream mpv plays, paced
against the playback clock. One tap, every platform, every output mode
(shared, bit-perfect, ALSA-direct): identical visual character
everywhere, volume-independent, no audio-server coupling, and ZERO
external dependencies (Qt's bundled ffmpeg libs ship with PySide6).
``ParallelDecodeTap`` (same pacing, ffmpeg-binary subprocess) remains
behind ``JT_VIS_TAP=ffmpeg`` for A/B until the Qt tap is verified on
both platforms, then dies. The retired ``MonitorAudioTap`` (PipeWire
sink-monitor capture, Linux-only) kept growing bug classes — default-
vs-pinned-sink mismatches, restart bounces on device moves, graph-rate
pinning under bit-perfect — see git history if a system-audio-reactive
mode is ever wanted again.

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


class QtDecodeTap(QObject):
    """In-process visualizer audio source — QtMultimedia's
    ``QAudioDecoder`` decoding the SAME stream mpv plays.

    Same contract and pacing model as ``ParallelDecodeTap`` (clock
    extrapolation between position ticks, hold-last-window when
    momentarily ahead, decay on pause) but with zero external
    dependencies: Qt's bundled ffmpeg libraries do the decode inside
    the process, so no ``ffmpeg`` binary, no subprocess, no pipe.

    Plumbing (all GUI-thread; verified by spike 2026-06-12):
    - http(s) sources stream through a ``QNetworkAccessManager`` reply
      handed to ``setSourceDevice`` (QAudioDecoder itself is not
      network-capable); ``file://`` blobs go straight to ``setSource``.
    - The decoder self-throttles: it stops decoding while its output
      buffer goes unread, so draining only below a small watermark
      bounds memory to ~2 s of mono PCM (~350 KB) — no flow-control
      device needed.
    - Decode runs O(1000)× realtime, so "seek" = restart the request
      and discard up to the target; cost is bounded by the network,
      not the CPU. Worker-thread seek requests cross to the GUI thread
      via a queued signal (Qt objects must not be touched off-thread).
    """

    _restart_requested = Signal()

    _WATERMARK_SAMPLES = 2 * 44100  # ~2 s of mono PCM buffered ahead
    _DRAIN_INTERVAL_MS = 150
    # Decode errors retry (corrupt data, transport blip) — the source
    # survives, so the tap self-heals at the live position.
    _RETRY_DELAY_MS = 1500
    _MAX_RETRIES = 3  # per source push; reset on every set_source

    def __init__(
        self,
        sample_rate: int = 44100,
        now_fn: Optional[Callable[[], float]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._sample_rate = int(sample_rate)
        self._now: Callable[[], float] = now_fn if now_fn is not None else time.monotonic
        self._started = False
        self._source = ""
        self._live = False
        # Pacing state (same fields/semantics as ParallelDecodeTap).
        self._target_ms: int = -1
        self._target_set_s: float = 0.0
        self._paused = False
        self._anchor_samples: int = 0
        self._consumed: int = 0
        self._last_window: Optional[NDArray] = None
        # Decoded-PCM hand-off: GUI thread appends, worker pops.
        import threading

        self._lock = threading.Lock()
        self._chunks: list = []  # list[np.ndarray], FIFO
        self._buffered: int = 0  # total samples across _chunks
        self._pending: Optional[NDArray] = None  # partial window remainder
        self._skip_remaining: int = 0  # decode-head discard for seeks
        # GUI-side decode objects (created lazily per source).
        self._dec = None  # QAudioDecoder
        self._reply = None  # QNetworkReply
        self._body = None  # QByteArray — the complete compressed body
        self._srcbuf = None  # QBuffer wrapping _body for the decoder
        self._drain_timer = None  # QTimer, created on first decode
        # One restart in flight at a time — the worker keeps detecting
        # the gap until the restart lands, and a queued storm of
        # restarts would each abort the previous decode.
        self._restart_pending = False
        self._retry_count = 0
        # False until the current decode delivers its first window —
        # gates the seek-restart heuristic (see __call__).
        self._delivered_since_begin = False
        from PySide6.QtCore import Qt

        self._restart_requested.connect(
            self._do_restart, Qt.ConnectionType.QueuedConnection
        )

    # ── lifecycle (GUI thread) ───────────────────────────────────────

    def start(self) -> None:
        self._started = True
        # Pre-warm the QtMultimedia plugin + ffmpeg backend: first
        # construction costs ~1s once per process, which otherwise
        # lands on the first track's bars (scenario test 2026-06-12 —
        # 1.1s first track, 40ms every track after).
        try:
            from PySide6.QtMultimedia import QAudioDecoder

            QAudioDecoder(self).deleteLater()
        except ImportError:
            pass
        if self._source and self._dec is None:
            self._begin_decode(max(0, self._target_ms))

    def stop(self, *, fast: bool = False) -> None:
        self._teardown_decode()

    def clear(self) -> None:
        """Playback stopped — drop the source so the tap goes silent."""
        self._teardown_decode()
        self._source = ""
        self._target_ms = -1
        self._target_set_s = 0.0
        self._last_window = None
        self._reset_buffers()

    # ── engine-facing state pushes (GUI thread; reads on worker) ────

    def set_source(self, source: str, *, start_ms: int = 0, live: bool = False) -> None:
        self._teardown_decode()
        self._source = source or ""
        self._live = bool(live)
        self._target_ms = int(start_ms)
        self._target_set_s = self._now()
        self._last_window = None
        self._retry_count = 0
        self._reset_buffers()
        if self._started and self._source:
            self._begin_decode(max(0, int(start_ms)))

    def set_target_ms(self, ms: int) -> None:
        self._target_ms = int(ms)
        self._target_set_s = self._now()

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        if not paused and self._target_set_s > 0.0:
            self._target_set_s = self._now()

    # ── decode plumbing (GUI thread) ─────────────────────────────────

    def _reset_buffers(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
            self._pending = None

    def _begin_decode(self, start_ms: int) -> None:
        """Acquire the compressed body for the current source, then run
        the decoder over it.

        http(s) sources download ONCE and keep the bytes for the whole
        track: Qt's ffmpeg backend treats any short read from a live
        sequential device as EOF/corruption, so the decode dies whenever
        it catches up to the network head (probe-time = the dead-bars-
        on-skip bug; catch-up sprints after seeks — drip-server repro
        2026-06-12). A complete in-RAM body decodes start-to-end with no
        failure mode AND makes seek-restarts network-free."""
        if not _numpy_available():
            return
        src = self._source
        if not src.startswith(("http://", "https://")):
            self._start_decoder(start_ms)
            return
        if self._body is not None:
            self._start_decoder(start_ms)
            return
        from PySide6.QtCore import QByteArray, QUrl
        from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

        from jellytoast.async_io import get_qnam

        reply = get_qnam().get(QNetworkRequest(QUrl(src)))
        self._reply = reply
        spool = bytearray()

        def _accumulate():
            spool.extend(bytes(reply.readAll()))

        def _complete():
            if self._reply is not reply:
                return  # superseded by a newer set_source
            self._reply = None
            reply.deleteLater()
            _accumulate()
            if reply.error() != QNetworkReply.NetworkError.NoError or not spool:
                logger.warning(
                    "QtDecodeTap: body fetch failed (%s) — will retry",
                    reply.errorString(),
                )
                from PySide6.QtCore import QTimer

                QTimer.singleShot(self._RETRY_DELAY_MS, self._retry_begin)
                return
            self._body = QByteArray(bytes(spool))
            spool.clear()
            # Decode from the LIVE target — the download took real
            # time and the track kept playing meanwhile.
            self._start_decoder(max(0, self._target_ms))

        # Drain the reply as it arrives (keeps QNAM's buffer flat),
        # decode once the body is complete.
        reply.readyRead.connect(_accumulate)
        reply.finished.connect(_complete)

    def _start_decoder(self, skip_ms: int) -> None:
        """Run a fresh QAudioDecoder from the held body (http) or the
        local file, discarding up to ``skip_ms``. Network-free, so seek
        restarts in either direction are cheap re-runs of this."""
        try:
            from PySide6.QtCore import QBuffer, QTimer, QUrl
            from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
        except ImportError:
            if not getattr(QtDecodeTap, "_warned_missing", False):
                logger.warning(
                    "QtMultimedia unavailable — the visualizer audio "
                    "tap can't decode (broken Qt install?)."
                )
                QtDecodeTap._warned_missing = True  # type: ignore[attr-defined]
            return

        self._teardown_decoder()
        rate = self._sample_rate
        self._anchor_samples = int(skip_ms * rate / 1000)
        self._consumed = 0
        self._skip_remaining = self._anchor_samples
        self._delivered_since_begin = False
        self._reset_buffers()

        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        dec = QAudioDecoder(self)
        dec.setAudioFormat(fmt)
        dec.bufferReady.connect(self._drain)
        dec.error.connect(self._on_decode_error)
        self._dec = dec

        if self._drain_timer is None:
            t = QTimer(self)
            t.setInterval(self._DRAIN_INTERVAL_MS)
            t.timeout.connect(self._drain)
            self._drain_timer = t
        self._drain_timer.start()

        if self._body is not None:
            # Wraps the held QByteArray by reference — no copy; the
            # array outlives the buffer because we hold it until the
            # next set_source/clear/stop.
            buf = QBuffer(self._body, dec)
            buf.open(QBuffer.OpenModeFlag.ReadOnly)
            self._srcbuf = buf
            dec.setSourceDevice(buf)
        else:
            src = self._source
            # file:// URLs parse correctly through QUrl on every OS
            # (including Windows drive letters); bare paths wrap.
            url = QUrl(src) if "://" in src else QUrl.fromLocalFile(src)
            dec.setSource(url)
        dec.start()

    def _teardown_decoder(self) -> None:
        """Stop the decoder only — the downloaded body survives so a
        restart never re-downloads."""
        dec, self._dec = self._dec, None
        # Parented to the decoder — deleteLater below reaps it; drop
        # our reference so it frees with it.
        self._srcbuf = None
        if self._drain_timer is not None:
            self._drain_timer.stop()
        if dec is not None:
            try:
                dec.stop()
            except Exception:
                pass
            dec.deleteLater()

    def _teardown_decode(self) -> None:
        """Full teardown: decoder, in-flight download, AND the held
        body (source is changing or the tap is stopping)."""
        self._teardown_decoder()
        reply, self._reply = self._reply, None
        self._body = None
        if reply is not None:
            try:
                reply.abort()
            except Exception:
                pass
            reply.deleteLater()

    @Slot()
    def _drain(self) -> None:
        """Pull decoded buffers while under the watermark. The decoder
        self-throttles while we leave buffers unread, so this IS the
        backpressure mechanism."""
        dec = self._dec
        if dec is None:
            return
        import numpy as np

        while dec.bufferAvailable():
            with self._lock:
                if self._buffered >= self._WATERMARK_SAMPLES:
                    return
            buf = dec.read()
            if not buf.isValid():
                return
            data = buf.data()
            try:
                raw = bytes(data.asarray(buf.byteCount()))
            except AttributeError:
                raw = bytes(data)
            arr = np.frombuffer(raw, dtype=np.float32)
            if self._skip_remaining > 0:
                if arr.size <= self._skip_remaining:
                    self._skip_remaining -= arr.size
                    continue
                arr = arr[self._skip_remaining:]
                self._skip_remaining = 0
            with self._lock:
                self._chunks.append(arr)
                self._buffered += arr.size

    @Slot()
    def _on_decode_error(self, *args) -> None:
        dec = self._dec
        msg = dec.errorString() if dec is not None else "unknown"
        self._teardown_decoder()
        # Self-heal: the source (and body) survive, so retry at the
        # live position instead of staying dead until the next track.
        # Capped — a persistently corrupt body would loop forever.
        self._retry_count += 1
        if self._retry_count > self._MAX_RETRIES:
            logger.warning("QtDecodeTap: decode error: %s (giving up)", msg)
            return
        logger.warning("QtDecodeTap: decode error: %s (will retry)", msg)
        from PySide6.QtCore import QTimer

        QTimer.singleShot(self._RETRY_DELAY_MS, self._retry_begin)

    @Slot()
    def _retry_begin(self) -> None:
        if self._started and self._source and self._dec is None and self._reply is None:
            self._begin_decode(max(0, self._target_ms))

    @Slot()
    def _do_restart(self) -> None:
        """Queued from the worker thread on a real seek — Qt decode
        objects must only be touched on the GUI thread. With the body
        held in RAM this is a decoder re-run, not a re-download; if the
        download is still in flight it arms at the live target on its
        own, so there's nothing to do here."""
        self._restart_pending = False
        if not (self._started and self._source):
            return
        if self._reply is None:
            self._begin_decode(max(0, self._target_ms))

    # ── sample hand-off (worker thread) ──────────────────────────────

    def _take_window(self) -> Optional[NDArray]:
        """Pop one _FFT_WINDOW of samples from the decoded queue, or
        None if not enough is buffered yet."""
        import numpy as np

        win = _FFT_WINDOW
        with self._lock:
            have = self._buffered + (self._pending.size if self._pending is not None else 0)
            if have < win:
                return None
            parts = [] if self._pending is None else [self._pending]
            got = 0 if self._pending is None else self._pending.size
            self._pending = None
            while got < win:
                chunk = self._chunks.pop(0)
                self._buffered -= chunk.size
                parts.append(chunk)
                got += chunk.size
            joined = parts[0] if len(parts) == 1 else np.concatenate(parts)
            out, rest = joined[:win], joined[win:]
            if rest.size:
                self._pending = rest
        return out

    def __call__(self) -> Optional[NDArray]:
        if not self._started or not self._source:
            return None
        rate = self._sample_rate
        win = _FFT_WINDOW
        target = None
        if self._target_ms >= 0 and not self._live:
            t_ms = float(self._target_ms)
            if not self._paused and self._target_set_s > 0.0:
                t_ms += (
                    min(
                        self._now() - self._target_set_s,
                        ParallelDecodeTap._EXTRAPOLATE_CAP_S,
                    )
                    * 1000.0
                )
            target = int(t_ms * rate / 1000)
        if target is not None:
            have = self._anchor_samples + self._consumed
            lead = have - target
            slop = ParallelDecodeTap._SLOP_WINDOWS * win
            seekish = abs(lead) > ParallelDecodeTap._RESTART_THRESHOLD_S * rate
            if seekish and (self._delivered_since_begin or lead > 0):
                # Real seek (either direction — checked BEFORE the hold
                # branch or a backward seek freezes the bars for the
                # length of the jump). Suppressed while a fresh decode
                # hasn't delivered yet AND the gap is behind-the-clock:
                # that's the download itself running long on a slow
                # link, and restarting would re-download from scratch
                # in a loop; once data lands, the discard path catches
                # up at ~22x realtime instead. A positive (ahead) gap
                # can only be a genuine backward seek, so it restarts
                # immediately.
                if not self._restart_pending:
                    self._restart_pending = True
                    self._restart_requested.emit()
                return self._last_window
            if lead > slop:
                if self._paused:
                    return None
                return self._last_window
            elif -lead > slop:
                deficit = min(
                    int(-lead // win) - 1,
                    ParallelDecodeTap._MAX_DISCARD_PER_TICK,
                )
                for _ in range(max(0, deficit)):
                    if self._take_window() is None:
                        break
                    self._consumed += win
        out = self._take_window()
        if out is None:
            return None
        self._consumed += win
        self._last_window = out
        self._delivered_since_begin = True
        return out


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
    Default (no callback): the engine owns a ``ParallelDecodeTap`` fed
    from the player bus — same tap on every platform and output mode.
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
        self._parallel_tap: Optional[ParallelDecodeTap] = None
        self._last_np: Optional[Any] = None
        self._last_pos_ms: int = 0
        if pcm_callback is None:
            # The single owned tap, every platform: an in-process
            # QtMultimedia decode of mpv's own stream (zero external
            # deps — Qt's bundled ffmpeg libs ship with PySide6).
            # JT_VIS_TAP=ffmpeg falls back to the ffmpeg-binary
            # subprocess tap for live A/B; delete it once the Qt tap
            # is verified on both platforms.
            import os

            if os.environ.get("JT_VIS_TAP") == "ffmpeg":
                self._parallel_tap = ParallelDecodeTap(sample_rate=sample_rate)
            else:
                self._parallel_tap = QtDecodeTap(sample_rate=sample_rate, parent=self)
            self._owned_tap = self._parallel_tap
            self._tap = self._parallel_tap
        else:
            self._owned_tap = None
            self._tap = pcm_callback
        self._sample_rate = int(sample_rate)
        self._band_count = int(band_count)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_FFTWorker] = None
        self._started = False
        self._bus = PlayerBus.get()
        # Casting plays on the receiver, not through local mpv — the
        # tap would otherwise keep decoding (and animating) a stream
        # nobody hears locally. Track the cast state so _on_bands_ready
        # can substitute a flat-zero vector while a cast is live; the
        # worker keeps running so playback-resume snaps back instantly.
        self._cast_active = bool(self._bus.cast_active)
        self._bus.cast_started.connect(self._on_cast_started)
        self._bus.cast_stopped.connect(self._on_cast_stopped)
        # Tap feed inputs (only meaningful when we own the tap).
        # position_updated is the tap's clock; playback_started carries
        # the source URL; pause state gates clock extrapolation.
        if self._parallel_tap is not None:
            self._bus.playback_started.connect(self._on_vis_playback_started)
            self._bus.position_updated.connect(self._on_vis_position)
            self._bus.playback_stopped.connect(self._on_vis_playback_over)
            self._bus.playback_ended.connect(self._on_vis_playback_over)
            self._bus.playback_paused.connect(
                lambda: self._parallel_tap.set_paused(True)
            )
            self._bus.playback_resumed.connect(
                lambda: self._parallel_tap.set_paused(False)
            )
            # The engine is lazy-built the first time the visualizer
            # opens — usually mid-track, AFTER playback_started fired —
            # so seed the source state from the live session. Without
            # this the tap sits sourceless (flat bars) until the next
            # track change.
            np_now = get_now_playing()
            if np_now is not None and getattr(np_now, "stream_url", ""):
                self._last_np = np_now
                self._last_pos_ms = int(getattr(np_now, "position", 0) or 0)
                self._parallel_tap.set_paused(
                    bool(getattr(np_now, "is_paused", False))
                )

    # ── Tap feed ──────────────────────────────────────────────────────

    def _on_vis_playback_started(self, np_obj) -> None:
        self._last_np = np_obj
        self._last_pos_ms = int(getattr(np_obj, "position", 0) or 0)
        if self._parallel_tap is not None:
            self._parallel_tap.set_paused(False)
            self._push_source(self._parallel_tap, np_obj, self._last_pos_ms)

    def _on_vis_position(self, ms: int) -> None:
        self._last_pos_ms = int(ms)
        if self._parallel_tap is not None:
            self._parallel_tap.set_target_ms(ms)

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

        if self._parallel_tap is not None:
            self._parallel_tap.start()
            # Feed the in-flight track so the tap serves from the first
            # frame (the engine is usually lazy-built mid-track).
            if self._last_np is not None:
                self._push_source(self._parallel_tap, self._last_np, self._last_pos_ms)
        elif self._owned_tap is not None:
            self._owned_tap.start()

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
            self._owned_tap.stop(fast=fast)

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
