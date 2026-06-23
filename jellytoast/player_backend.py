"""
mpv-based playback backend.

Why mpv:
- Bit-perfect audio (FLAC, ALAC, OPUS, DSD) — no transcoding
- Gapless playback for albums
- ReplayGain support
- Lower CPU/RAM than browser-based playback
- Native to Linux/Arch

This module exposes ``MpvController`` (a ``QObject``) — the headless audio
engine behind ``PlayerBus``. Beyond raw mpv playback it also owns:
- **Cast routing + status** (``# ── Cast routing ──``): dispatches
  play/pause/seek/volume to the active cast device and runs a 500 ms poll
  that marshals chromecast status back onto the GUI thread. Per-protocol
  transport lives in ``jellytoast.cast_manager``; this is the local-vs-cast
  routing seam.
- **Crossfade + gapless prefetch**: a second overlapping mpv handle for
  equal-power crossfades and next-track preloading (``jellytoast.crossfade``).
- **EQ apply, the sleep timer, and server progress reporting** (scrobble /
  now-playing position back to the provider).

The cross-thread cast-status plumbing (``_CastStatusSignal`` /
``_CastStatusForwarder`` / the poll) is the candidate for a future
``CastTransportBridge`` extraction, if this file ever grows again.
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from jellytoast.platform_compat import IS_LINUX, IS_MACOS, IS_WINDOWS

logger = logging.getLogger(__name__)

# ── Audio-output picker curation ──────────────────────────────────────
# Sink-server families whose per-device entries are real destinations.
_AUDIO_SINK_FAMILIES = ("pipewire", "pulse", "wasapi", "coreaudio")
# Which family's BARE entry ("Default (pipewire)") to keep — first one
# present wins; the rest are dropped (pulse duplicates pipewire on any
# pipewire box, and jack/openal/sdl/sndio defaults are dev backends).
_AUDIO_DEFAULT_FAMILY_ORDER = ("pipewire", "pulse", "wasapi", "coreaudio", "alsa")
# ALSA namehint prefixes that address actual hardware (the DAC-direct
# path). Everything else under alsa/ is a PCM plugin alias (lavrate,
# speex*, upmix, vdownmix, jack, oss, pipewire…), a sysdefault dupe, a
# surroundXY profile of the same jack, or a usbstream gadget endpoint.
_ALSA_DIRECT_PREFIXES = ("hw:", "plughw:", "front:", "hdmi:", "iec958:")


def _foreign_sink_inputs(pactl_text: str, own_pid: int) -> int:
    """Count OTHER apps' active (non-corked) playback streams in
    ``pactl list sink-inputs`` output. Our own stream (libmpv runs
    in-process, so its sink-input carries our pid) is excluded; a
    stream with no readable pid counts as foreign. Pure function for
    tests."""
    count = 0
    for block in pactl_text.split("Sink Input #")[1:]:
        if "Corked: yes" in block:
            continue
        m = re.search(r'application\.process\.id\s*=\s*"?(\d+)"?', block)
        if m and int(m.group(1)) == own_pid:
            continue
        count += 1
    return count


def _curate_audio_devices(devices: list) -> list:
    """Filter mpv's raw ``[(name, description), …]`` enumeration down to
    entries a user can sensibly pick: ``auto``, ONE backend default, the
    sink-server sinks (pulse/* dropped when the same sinks exist as
    pipewire/*), and direct-hardware ALSA devices. Pure function — order
    preserved from mpv."""
    names = [n for n, _ in devices]
    have_pipewire_sinks = any(n.startswith("pipewire/") for n in names)
    default_family = next(
        (
            f
            for f in _AUDIO_DEFAULT_FAMILY_ORDER
            if f in names or any(n.startswith(f + "/") for n in names)
        ),
        None,
    )
    out = []
    for name, desc in devices:
        if name == "auto":
            out.append((name, desc))
            continue
        family, sep, rest = name.partition("/")
        if not sep:
            if family == default_family:
                out.append((name, desc))
            continue
        if family in _AUDIO_SINK_FAMILIES:
            if family == "pulse" and have_pipewire_sinks:
                continue  # same sink, pipewire/* spelling already listed
            out.append((name, desc))
            continue
        if family == "alsa" and rest.startswith(_ALSA_DIRECT_PREFIXES):
            out.append((name, desc))
    return out

# Windows + frozen (Inno .exe / MSIX): libmpv-2.dll ships next to the frozen
# executable (PyInstaller dest '.'). python-mpv only probes PATH and its own
# module dir, so register the exe dir on the DLL search path — otherwise the
# import fails under MSIX's read-only package layout (no audio). The Inno
# build already finds it next-to-exe; this is harmless there and a no-op
# elsewhere.
if IS_WINDOWS and getattr(sys, "frozen", False):
    try:
        os.add_dll_directory(os.path.dirname(sys.executable))
    except OSError:
        pass

# AppImage (Linux, frozen): the bundle vendors its own libmpv under usr/lib (the
# .deb gets libmpv from the host via Depends; an AppImage can't, so it must
# self-contain it). python-mpv loads libmpv at import via
# ``ctypes.util.find_library("mpv")`` then ``CDLL(<that>)`` — and find_library
# only scans the SYSTEM ldconfig cache, so it never sees a lib inside a mounted
# AppImage. Redirect find_library at the bundled copy (soname-agnostic: Ubuntu
# 22.04 ships libmpv.so.1, newer distros .so.2). AppRun also puts usr/lib on
# LD_LIBRARY_PATH so libmpv's own FFmpeg deps resolve. APPDIR is set only by the
# AppImage runtime, so this is a no-op for the .deb / pipx / source runs.
_appdir = os.environ.get("APPDIR")
if IS_LINUX and getattr(sys, "frozen", False) and _appdir:
    import ctypes.util
    import glob

    _bundled = sorted(glob.glob(os.path.join(_appdir, "usr", "lib", "libmpv.so*")))
    if _bundled:
        _mpv_lib = _bundled[-1]  # highest soname version present
        _orig_find_library = ctypes.util.find_library

        def _find_library(name, _orig=_orig_find_library, _mpv=_mpv_lib):
            return _mpv if name == "mpv" else _orig(name)

        ctypes.util.find_library = _find_library

# macOS (frozen .app): the bundle vendors its own libmpv*.dylib (there is no
# system libmpv on a clean Mac — same situation as the Windows DLL and the
# AppImage). python-mpv loads libmpv at import via
# ``ctypes.util.find_library("mpv")`` then ``CDLL(<that>)``, and on macOS that
# helper searches the standard system dylib paths ONLY — never the .app bundle
# — while the hardened runtime / SIP strips DYLD_* env vars from the notarized
# binary, so a DYLD_LIBRARY_PATH redirect is unreliable. So redirect
# find_library straight at the bundled copy (same approach as the AppImage
# branch above). PyInstaller drops the dylib at the COLLECT root, which is
# sys._MEIPASS at run time (Contents/Frameworks in the .app); also check the
# executable dir as a fallback. No-op for source / pip / Homebrew runs (not
# frozen), where the system/Homebrew libmpv resolves normally.
if IS_MACOS and getattr(sys, "frozen", False):
    import ctypes.util
    import glob

    _search = [
        d
        for d in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable))
        if d
    ]
    _bundled = []
    for _d in _search:
        _bundled += glob.glob(os.path.join(_d, "libmpv*.dylib"))
    if _bundled:
        _mpv_lib = sorted(_bundled)[-1]  # highest version present (libmpv.2.dylib)
        _orig_find_library = ctypes.util.find_library

        def _find_library(name, _orig=_orig_find_library, _mpv=_mpv_lib):
            return _mpv if name == "mpv" else _orig(name)

        ctypes.util.find_library = _find_library

try:
    import mpv

    MPV_AVAILABLE = True
except (ImportError, OSError) as e:
    MPV_AVAILABLE = False
    _MPV_ERROR = str(e)

from jellytoast.async_io import run_async
from jellytoast.cast_manager import CastType
from jellytoast.playback.crossfade import Crossfader, CrossfadeState
from jellytoast.player_cast_transport import (
    _CastStatusForwarder,
    _CastStatusSignal,
    _CastTransportMixin,
)
from jellytoast.player_state import NowPlaying, PlayerBus, get_now_playing
from jellytoast.providers import get_provider
from jellytoast.settings import get_settings


class MpvController(_CastTransportMixin, QObject):
    """
    Single mpv instance, managed for the whole application.
    Headless by default; a video widget can attach to it later.
    """

    # Internal cross-thread signals (mpv callbacks fire on bg threads)
    _emit_position = Signal(int)
    _emit_duration = Signal(int)
    _emit_paused = Signal(bool)
    _emit_ended = Signal()
    _emit_play_error = Signal()
    _emit_streaming_info = Signal(str, int)  # (codec, kbps)
    _emit_radio_title = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_provider()
        self._mpv: Optional["mpv.MPV"] = None
        self._last_progress_report = 0
        # Optional CastManager — when set with an active_cast, transport
        # signals (play/pause/seek/volume) route to the cast device
        # instead of mpv. Wired in main() after both controllers exist.
        self._cast_manager = None
        # Cast status feed — two paths feed _on_position / _on_duration /
        # _on_paused / _on_ended:
        #  1. Push: register a status listener on cc.media_controller.
        #     pychromecast fires it (on its own worker thread) every
        #     time the receiver sends a MEDIA_STATUS update — typically
        #     ~1 Hz during playback, plus on every state transition.
        #  2. Poll: 500ms QTimer that re-reads cached mc.status. Belt
        #     and suspenders for receivers that don't push reliably,
        #     and to interpolate between push updates.
        self._cast_status_signal = _CastStatusSignal()
        self._cast_status_signal.status.connect(self._on_cast_status_push)
        self._cast_status_listener = _CastStatusForwarder(self._cast_status_signal)
        self._cast_listener_attached_to = None  # cast_object we registered on
        # Monotonic counter for cast attempts. Each cast_to_chromecast_async
        # call captures the value at dispatch time; the on_done callback
        # short-circuits if a newer attempt has fired since. Without this,
        # mashing Next while casting fires playback_started for stale
        # tracks (the older callback completes after the newer one started).
        self._cast_attempt = 0
        # The poll timer is gated on bus.cast_started / bus.cast_stopped
        # — no point waking every 500ms when nothing is casting. Started
        # from `_on_cast_started`, stopped from `_on_cast_stopped`.
        self._cast_poll_timer = QTimer(self)
        self._cast_poll_timer.setInterval(500)
        self._cast_poll_timer.timeout.connect(self._poll_cast_status)
        self._cast_last_player_state = None
        self._cast_last_duration_ms = -1
        self._cast_last_position_ms = -1
        # Anchor for local interpolation between chromecast status
        # pushes — pychromecast only updates mc.status.current_time on
        # state changes (play / pause / seek / load), not every second
        # of playback. Without interpolation the progress bar would
        # only tick when the user pauses or skips. We extrapolate from
        # the last anchored position using monotonic wall time.
        import time as _time_mod

        self._monotonic = _time_mod.monotonic
        self._cast_anchor_pos_ms = 0
        self._cast_anchor_wall = 0.0

        # Server progress reporting is now driven by the `time-pos` mpv
        # property observer, gated by a position delta. This anchor is
        # the position (ms) of the most-recently-sent update; we only
        # POST when the head has moved at least PROGRESS_REPORT_DELTA_MS
        # since the last report, plus once on every pause/resume toggle.
        # Replaces the old 10s wall-clock timer that double-reported
        # during pause and lagged the actual playhead by up to 10s.
        self._last_reported_position_ms = -1

        # Per-play session bookkeeping for Jellyfin's Sessions/Playing
        # reports. Jellyfin pairs Start → Progress → Stop calls by the
        # client-supplied PlaySessionId GUID; without it the server
        # creates ghost rows in the admin Sessions view and can't
        # dedupe progress against starts. We mint a fresh GUID every
        # time we issue a Playing report and carry it through to the
        # matching Stopped. PlayMethod is derived from the user's
        # audio_quality preference: "original" → DirectStream (server
        # ships bytes verbatim), anything else → Transcode (server
        # encodes on the fly to MP3).
        self._session_item_id: str = ""
        self._session_id: str = ""
        self._session_play_method: str = "DirectStream"

        # Last applied (enabled, bands_tuple, preamp_db) tuple for the
        # EQ filter chain. ``apply_eq`` compares against this to short-
        # circuit when nothing has changed — slider drags coalesce to a
        # single `eq_changed` per tick, but the playback_started re-
        # apply path can fire on every track and would otherwise rebuild
        # the filter graph for an unchanged curve. ``None`` means
        # "never applied" so the first call after launch always writes
        # through. The pre-amp dB is part of the cache key so a slider
        # drag on the pre-amp alone re-applies even when bands didn't
        # change.
        self._last_eq_state: Optional[tuple] = None

        # Runtime bit-perfect state. The "Bit Perfect" claim requires
        # ALL of: bit_perfect_mode setting on, audio_quality=="original"
        # so the server isn't transcoding, and the source codec being
        # itself lossless. The setting being on is necessary but not
        # sufficient — playing an MP3 with the toggle checked is still
        # lossy in, lossy out. ``set_volume`` gates the 100-pin against
        # this *runtime* state (not the raw setting), so the volume
        # popup unlocks for lossy tracks rather than ineffectually
        # pinning a slider that can't preserve a contract that doesn't
        # exist. ``_current_codec`` mirrors mpv's last ``audio-codec-name``
        # report; ``_refresh_bit_perfect_active`` recomputes + broadcasts
        # the runtime flag whenever a contributing input changes.
        self._current_codec: str = ""
        # Casting forces the runtime contract to False — mpv's local
        # filter chain isn't what's playing. ``_cast_active_flag`` is
        # the master gate inside ``_compute_bit_perfect_active``; the
        # cast_started / cast_stopped handlers flip it.
        self._cast_active_flag: bool = False
        self.bus.bit_perfect_changed.connect(self._on_bit_perfect_setting_changed)
        self.bus.audio_quality_changed.connect(self._on_audio_quality_changed)
        self.bus.cast_started.connect(self._on_cast_started_bit_perfect)
        self.bus.cast_stopped.connect(self._on_cast_stopped_bit_perfect)
        # Launch state — when the user persists ``bit_perfect_mode=True``
        # and ``audio_quality="original"`` across restarts, we don't yet
        # know the codec (first mpv report lands ~2 s into first
        # playback). Default the runtime flag to True in that case so
        # the volume lock honours the user's intent during the launch
        # window; the first codec report will drop the flag back to
        # False if the source turns out to be lossy.
        #
        # Emit the signal too, not just the bare attribute write —
        # MpvController is constructed in ``_post_show_init`` AFTER the
        # NowPlayingBar (and its VolumeButton) is built, so
        # VolumeButton's ``_refresh_tooltip_for_bit_perfect`` already
        # ran reading the class-default False and stamped the wrong
        # tooltip. The emit catches every slot that connected during
        # the earlier window-construction pass.
        if (
            self.settings.bit_perfect_mode
            and (self.settings.audio_quality or "").strip().lower() == "original"
        ):
            self.bus.bit_perfect_active = True
            self.bus.bit_perfect_active_changed.emit(True)

        # Gapless prefetch state. `_prefetched_url` is the URL we asked
        # mpv to append to its internal playlist as the "next" track.
        # When mpv ends the current entry, libmpv silently moves to it
        # without an audio gap. Cleared on every explicit play() call
        # because mpv.play() resets the playlist; QueueManager re-emits
        # `queue_prefetch_request` immediately afterwards so the slot
        # is repopulated. `_prefetched_item_id` is the matching media id
        # — the auto-advance handoff in `play()` uses it to recognize
        # the same track even when the provider mints a fresh URL on
        # the next get_audio_stream_url call (Subsonic / Navidrome
        # rotate salt + token per request, so URL string equality fails
        # there even though mpv is already gaplessly decoding the right
        # track).
        self._prefetched_url: Optional[str] = None
        self._prefetched_item_id: Optional[str] = None

        # Sleep timer — session-scoped countdown. `_sleep_timer` is the
        # QTimer that fires on elapse; owned by `self` so it dies with the
        # backend. `_sleep_on_fire` is the action to take when it elapses
        # ("pause" | "end_of_track" | "fade_stop"). `_sleep_pending_eot`
        # arms the end-of-track path: the timer has already elapsed, and
        # the *next* playback_ended emit triggers pause.
        self._sleep_timer: Optional[QTimer] = None
        self._sleep_on_fire: str = "pause"
        self._sleep_pending_eot: bool = False
        # Fade-to-stop ramp state. `_sleep_fade_timer` ticks every
        # _SLEEP_FADE_TICK_MS until the volume reaches zero, at which
        # point we pause and restore `_sleep_fade_original_volume` so
        # the next play session isn't silent.
        self._sleep_fade_timer: Optional[QTimer] = None
        self._sleep_fade_original_volume: Optional[int] = None
        self._sleep_fade_steps_remaining: int = 0
        self._sleep_fade_step_decrement: float = 0.0
        self._sleep_fade_current_volume: float = 0.0
        # When True (default), the fade pauses + restores volume on
        # reaching zero. When False (the EOT-pending fade-at-track-end
        # path), the ramp just stops at zero and leaves the EOT pause
        # handler to do the restore on the natural playback_ended.
        self._sleep_fade_pause_on_complete: bool = True
        # Pre-mute volume for the save/restore mute scheme. mute is
        # implemented as volume=0 (not mpv["mute"]) so the PipeWire
        # node's mute flag is never toggled — WirePlumber's restore-
        # stream module persists per-app PW mute and re-applies it on
        # every launch, leaving the app silently muted across sessions
        # if we'd ever flipped the PW flag. None = currently unmuted.
        self._muted_volume: Optional[int] = None

        # Live cast-device volume (0-100), tracked from our own outbound
        # changes (connect-time initial + slider). settings.volume is the
        # LOCAL-playback baseline and deliberately diverges from the cast
        # level (set_volume skips the persist while casting), so cast
        # mute/unmute must save & restore THIS, not settings.volume — else
        # unmute jumps the speaker to the local baseline (often 100%)
        # instead of the level you were actually hearing.
        self._cast_volume: int = self._CAST_INITIAL_VOLUME
        # Monotonic timestamp of our last OUTBOUND cast-volume write. The
        # receiver echoes our writes back (and lags a poll or two), so the
        # poll's receiver-volume sync ignores device reports for a short
        # window after a push — otherwise the slider bounces to the stale
        # value, and a post-connect echo could snap the level back up to a
        # previous session's (possibly loud) volume. See _poll_cast_status.
        self._cast_volume_push_wall: float = 0.0

        # Crossfade scaffolding. Built lazily on first `_on_position` so
        # tests + cold-launch don't pay for a sibling handle that may
        # never fire. Gated by the ``crossfade_enabled`` runtime setting.
        # The ``QueueManager._build_now_playing`` payload exposes the
        # current queue item's raw dict via ``NowPlaying.raw`` — we
        # snapshot that here for the Crossfader's same-album check.
        self._crossfader: Optional[Crossfader] = None
        self._current_duration_ms: int = 0

        if not MPV_AVAILABLE:
            logger.error("mpv unavailable: %s", _MPV_ERROR)
            logger.error(
                "Install mpv from your package manager or https://mpv.io."
            )
            return

        self._init_mpv()
        self._connect_bus()

    # ── mpv setup ───────────────────────────────────────────────────────────

    def _make_mpv_handle(self, *, volume: Optional[int] = None) -> "mpv.MPV":
        """Build a fresh mpv handle with jellytoast's standard options.
        Extracted so the Crossfader sibling handle in
        ``jellytoast/playback/crossfade.py`` can mint a second instance with
        the same decoder config (codec coverage, ReplayGain, audio-client
        name) without copy-pasting the option block. The caller is
        responsible for attaching property observers + event callbacks
        specific to its role; this factory only constructs.

        ``volume`` defaults to the user's persisted volume (matches the
        primary handle's behavior); the Crossfader passes ``0`` because
        its sibling starts silent and ramps up."""
        kwargs = dict(
            ytdl=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            keep_open="no",
            idle="yes",
            force_window="no",
            audio_display="no",  # no album-art on its own video output
            hwdec="auto-safe",
            cache="yes",
            demuxer_max_bytes="100MiB",
            volume=self.settings.volume if volume is None else int(volume),
            replaygain=self.settings.replaygain,
            replaygain_clip="no",
            audio_client_name="jellytoast",
        )
        kwargs["gapless_audio"] = "weak"
        kwargs["prefetch_playlist"] = "yes"
        # Windows track-change click: a loadfile-replace transition closes +
        # reopens the WASAPI output, and that device re-acquire clicks/buzzes
        # (shared OR exclusive). Streaming silence keeps the output device open
        # across the reinit, which removes the click — and also the ~150 ms
        # WASAPI-exclusive post-unpause clip (mpv#17156). WINDOWS-ONLY: the
        # reopen click is WASAPI-specific, and upstream discourages this option
        # in general (it alters underrun/seek handling), so PipeWire/CoreAudio
        # are left untouched. Trade-off in exclusive (bit-perfect) mode: the
        # device stays acquired while paused, so other apps can't play audio
        # until playback is stopped (not merely paused). JT_NO_AUDIO_SILENCE
        # forces it off. NOTE: does NOT remove the irreducible reopen on a
        # genuine sample-rate change in exclusive mode — that needs a fresh
        # WASAPI client by definition.
        if IS_WINDOWS and not os.environ.get("JT_NO_AUDIO_SILENCE"):
            kwargs["audio_stream_silence"] = "yes"
        # Audiophile T3 — opt-in exclusive output. WASAPI Exclusive on
        # Windows, HogMode on macOS, sink-cork on PipeWire. Gated behind
        # bit_perfect_mode so an accidental flick doesn't silence every
        # other app on the machine. See
        # ``docs/research/bit_perfect_playback.md`` §7.
        # Also gated on audio_quality=="original": exclusive output is
        # only meaningful when the server is shipping the source bits
        # untouched. A transcoded stream + exclusive lock is the worst
        # of both worlds (every other app silenced, contract already
        # broken). The settings dialog greys the quality combo while
        # bit-perfect is on, but a defence-in-depth gate here covers
        # the edge case of a stale persisted value mid-upgrade.
        if (
            self.settings.bit_perfect_mode
            and self.settings.audio_exclusive
            and (self.settings.audio_quality or "").strip().lower() == "original"
        ):
            kwargs["audio_exclusive"] = "yes"
        # User-pinned output device (Settings → Playback → Audio output).
        # "auto" = let mpv pick (the long-standing default). Anything
        # else is an mpv audio-device-list name — pipewire/pulse sinks,
        # WASAPI endpoints, or a raw ALSA hw: device for the direct
        # audiophile path (docs/research/audio_output_routing.md).
        _out_dev = self.settings.audio_output_device
        if _out_dev and _out_dev != "auto":
            kwargs["audio_device"] = _out_dev
        # Layered open fallback — the constructor must never leave the
        # app without audio:
        #  1. A pinned device can vanish (USB DAC unplugged, stale
        #     persisted name) — drop it, retry on auto.
        #  2. Exclusive open can be refused (mpv issues #11600/#11733 on
        #     some Windows DACs) — drop the flag, retry shared.
        # Each step logs so the degradation is visible in the console;
        # the persisted settings are left alone (the device may be back
        # next launch).
        try:
            return mpv.MPV(**kwargs)
        except Exception as e:
            if "audio_device" not in kwargs and "audio_exclusive" not in kwargs:
                raise
            if "audio_device" in kwargs:
                logger.warning(
                    "mpv open with audio-device=%r failed (%s) — retrying on auto",
                    kwargs.pop("audio_device"),
                    e,
                )
                try:
                    return mpv.MPV(**kwargs)
                except Exception as e2:
                    if "audio_exclusive" not in kwargs:
                        raise
                    e = e2
            logger.warning(
                "mpv audio-exclusive open failed (%s) — retrying in shared mode",
                e,
            )
            kwargs.pop("audio_exclusive", None)
            return mpv.MPV(**kwargs)

    def _init_mpv(self):
        self._mpv = self._make_mpv_handle()
        # Clear any stale PW-node mute that WirePlumber's restore-stream
        # may have re-applied on the new stream node (see
        # `_muted_volume` note in __init__). Our mute uses volume save/
        # restore, so the node's mute flag should always be false.
        try:
            self._mpv["mute"] = False
        except Exception:
            pass

        # audio-bitrate updates per decode tick — far more often than the
        # UI needs. Rate-limit to one emit every 2s so the user sees a
        # stable readout. Controller-level state (shared across handles)
        # so a crossfade swap doesn't reset the throttle.
        self._streaming_info_throttle_s = 2.0
        self._last_streaming_emit_t = 0.0
        # Live ICY title from internet-radio streams; filtered to real
        # transitions in the observer below.
        self._last_radio_title = ""
        # Handles that already carry our observers (id-keyed). The active
        # handle and the Crossfader's dormant sibling ping-pong, so both
        # end up observed; the per-observer gate keeps only the active
        # one live.
        self._observed_handle_ids: set = set()
        self._attach_handle_observers(self._mpv)

        # Wire cross-thread signals to bus (Qt-thread safe). One-time —
        # these signaler→handler links are controller-level, not
        # per-handle, so they must NOT live in _attach_handle_observers.
        self._emit_position.connect(self._on_position)
        self._emit_duration.connect(self._on_duration)
        self._emit_paused.connect(self._on_paused)
        self._emit_ended.connect(self._on_ended)
        self._emit_play_error.connect(self._on_playback_error)
        self._emit_streaming_info.connect(self._on_streaming_info)
        self._emit_radio_title.connect(self._on_radio_title)

    def _attach_handle_observers(self, handle) -> None:
        """Attach time-pos / duration / pause / end-file / audio-bitrate /
        icy-title observers to ``handle``. Idempotent per handle.

        Every observer is gated on ``handle is self._mpv`` so only the
        ACTIVE handle's events propagate. The Crossfader's dormant sibling
        gets observers too (it becomes the active handle on a SWAP) but
        stays silent while it isn't current — during a fade both handles
        play, and only the logically-current one should drive the seek
        bar / EOF advance.

        Re-attaching here is what makes a crossfade SWAP work: the sibling
        is minted by ``_make_mpv_handle`` (which attaches nothing), so
        without this the post-swap active handle would have NO observers —
        seek bar frozen, no end-file → queue stalls at EOF (the player
        would otherwise hang on the first cross-album fade)."""
        if handle is None:
            return
        ids = getattr(self, "_observed_handle_ids", None)
        if ids is None:
            ids = self._observed_handle_ids = set()
        if id(handle) in ids:
            return
        ids.add(id(handle))

        @handle.property_observer("time-pos")
        def _on_time(_name, value):
            if handle is not self._mpv:
                return
            if value is not None:
                self._emit_position.emit(int(value * 1000))

        @handle.property_observer("duration")
        def _on_dur(_name, value):
            if handle is not self._mpv:
                return
            if value is not None:
                self._emit_duration.emit(int(value * 1000))

        @handle.property_observer("pause")
        def _on_pause(_name, value):
            if handle is not self._mpv:
                return
            self._emit_paused.emit(bool(value))

        @handle.event_callback("end-file")
        def _on_end(event):
            if handle is not self._mpv:
                return
            try:
                reason = event.data.reason
                # mpv ABI: 0=eof, 2=stop, 3=quit, 4=error, 5=redirect
                if reason in ("eof", 0):
                    self._emit_ended.emit()
                elif reason in ("error", 4):
                    # A track that ERRORS (e.g. the audio output refused
                    # to open — exclusive-on-PipeWire fails every open)
                    # used to be silently ignored: the queue showed a
                    # playing track that wasn't. Route to the recovery
                    # handler on the GUI thread.
                    self._emit_play_error.emit()
            except Exception:
                pass

        @handle.property_observer("audio-bitrate")
        def _on_audio_bitrate(_name, value):
            if handle is not self._mpv:
                return
            if not value or value < 1000:
                return
            now = time.monotonic()
            if now - self._last_streaming_emit_t < self._streaming_info_throttle_s:
                return
            self._last_streaming_emit_t = now
            try:
                codec = handle.audio_codec_name or ""
            except Exception:
                codec = ""
            kbps = int(value / 1000)
            self._emit_streaming_info.emit(codec, kbps)

        # Live ICY title from internet-radio streams. mpv populates
        # ``metadata/by-key/icy-title`` for Icecast / Shoutcast feeds —
        # typically ``"Artist - Track"`` but station-specific. Manual
        # coverage only (needs a real network stream); see
        # docs/manual_test_plan.md §4 "Internet radio".
        @handle.property_observer("metadata/by-key/icy-title")
        def _on_icy_title(_name, value):
            if handle is not self._mpv:
                return
            title = (value or "").strip()
            if not title or title == self._last_radio_title:
                return
            self._last_radio_title = title
            logger.debug("icy-title: %r", title)
            self._emit_radio_title.emit(title)

    def _connect_bus(self):
        self.bus.play_requested.connect(self.play)
        self.bus.pause_toggled.connect(self.toggle_pause)
        self.bus.stop_requested.connect(self.stop)
        self.bus.seek_requested.connect(self.seek)
        self.bus.seek_relative.connect(self.seek_relative)
        self.bus.volume_changed.connect(self.set_volume)
        self.bus.mute_toggled.connect(self.toggle_mute)
        self.bus.replaygain_changed.connect(self.set_replaygain)
        self.bus.eq_changed.connect(self.apply_eq)
        self.bus.audio_exclusive_changed.connect(self.set_audio_exclusive)
        self.bus.audio_output_device_changed.connect(self.set_audio_output_device)
        # Re-apply the EQ filter at the head of every new track —
        # mpv's filter graph survives loadfile-replace in current
        # builds, but the `gapless_audio=weak` path occasionally
        # drops the graph when the sample rate changes across
        # tracks (44.1 → 48 transitions in particular). Re-asserting
        # the chain on playback_started is cheap when nothing has
        # changed (idempotent via ``_last_eq_state``) and keeps the
        # user's curve audible even when mpv re-plugs the output.
        self.bus.playback_started.connect(self._reapply_eq_on_start)
        # If a sleep-fade-at-end-of-track was mid-ramp when the user
        # skipped (or any other path advanced the queue), cancel it +
        # restore the volume. ``_arm_sleep_eot_fade_if_due`` will re-arm
        # a fresh fade on the new track's last N seconds while
        # ``_sleep_pending_eot`` stays set; without this reset, the
        # new track would play at a mid-fade level.
        self.bus.playback_started.connect(self._on_track_started_sleep_fade_reset)
        self.bus.cast_started.connect(self._on_cast_started)
        self.bus.cast_stopped.connect(self._on_cast_stopped)
        self.bus.queue_prefetch_request.connect(self._on_prefetch_request)
        self.bus.crossfade_started.connect(self._on_crossfade_started)
        self.bus.playback_ended.connect(self._on_sleep_eot_check)
        self.bus.sleep_timer_requested.connect(self.start_sleep_timer)
        self.bus.sleep_timer_cancel_requested.connect(self.cancel_sleep_timer)

    def _reset_mpv_start(self):
        """Clear mpv's start offset after a deferred handoff load. If
        we don't, the next track the user skips to inherits the offset
        (Next on a 4-min track after resuming at 1:30 → new track plays
        from 1:30 instead of 0:00). Called via QTimer to ensure mpv has
        already consumed the offset for the current load."""
        if self._mpv is None:
            return
        try:
            self._mpv["start"] = "none"
        except Exception:
            pass

    # ── Playback session bookkeeping ────────────────────────────────────────

    def _resolve_play_method(self) -> str:
        """Derive the Jellyfin PlayMethod from the user's audio quality
        preference. "original" means we hand mpv `/Audio/{id}/stream`
        with `static=true` — the server ships bytes verbatim, mpv
        decodes locally → DirectStream. Any other value forces
        `/stream.mp3` with a MaxStreamingBitrate, which is a server-
        side transcode → Transcode. PlayMethod misreporting skews the
        admin transcoding stats so getting this right matters."""
        # Empty normalizes to "original" — matches get_audio_stream_url
        # (6b8a944): an empty audio_quality is direct-play, so reporting
        # Transcode here would mis-skew the admin stats (whole-class sweep of
        # that fix; latent today but a real un-swept sibling gate).
        quality = (get_settings().audio_quality or "original").strip().lower()
        if quality == "original":
            return "DirectStream"
        return "Transcode"

    def _begin_play_session(self, np: NowPlaying):
        """Stamp a fresh PlaySessionId for the upcoming /Sessions/Playing
        report and capture the matching PlayMethod. If a session is
        already in flight (e.g. queue auto-advance), emit a Stopped
        for that outgoing item first so the server doesn't time the
        previous session out at 60s and double-attribute play counts."""
        self._end_play_session_if_active(force_finished=False)
        self._session_item_id = np.item_id
        self._session_id = uuid.uuid4().hex
        self._session_play_method = self._resolve_play_method()
        # Every fresh play re-arms the audio-health watchdog — a dead
        # AO shows up here too (e.g. the first open after a device
        # change), not just at the device-switch moment. Do NOT reset
        # the stage here: during an untimed race tracks restart faster
        # than the check interval, and a reset would pin recovery at
        # stage 0 forever (the 2026-06-11 'still does it' report). The
        # stage only resets on a HEALTHY check.
        self._schedule_audio_health_check()

    def _end_play_session_if_active(self, force_finished: bool = False):
        """Send a final Stopped for the current session and clear the
        session id. `force_finished` is True when the track played
        through to completion — we report PositionTicks at the
        track's known duration so the server's "watched %" math
        crosses the auto-played threshold cleanly."""
        if not self._session_item_id:
            return
        np = get_now_playing()
        # Use the live now-playing only if it still represents the
        # session item — otherwise report a position from whatever we
        # last knew (best effort; the session is closing either way).
        if np.item_id == self._session_item_id:
            position_ticks = np.duration_ticks if force_finished else np.position_ticks
        else:
            position_ticks = 0
        # Fire-and-forget on the pool — see _report_session_start.
        iid, sid = self._session_item_id, self._session_id
        pm = self._session_play_method
        run_async(
            lambda: self.api.report_playback_stopped(
                iid, position_ticks, play_session_id=sid, play_method=pm
            ),
            on_error=lambda _e: None,
        )
        self._session_item_id = ""
        self._session_id = ""
        self._session_play_method = "DirectStream"

    def _report_session_start(self, np: NowPlaying):
        """Wrapper around api.report_playback_start that always pulls
        the session id + play method we minted in _begin_play_session.

        Fire-and-forget on the shared pool — playback reporting is a
        provider HTTP call (Subsonic's is a blocking requests.get), and
        a dead/unreachable server must never stall the GUI thread for
        the request's timeout. Snapshot the values now; the pool thread
        just sends them."""
        iid, pos = np.item_id, np.position_ticks
        sid, pm = self._session_id, self._session_play_method
        run_async(
            lambda: self.api.report_playback_start(iid, pos, play_session_id=sid, play_method=pm),
            on_error=lambda _e: None,
        )

    # ── Playback control ────────────────────────────────────────────────────

    @Slot(object)
    def play(self, np: NowPlaying):
        if not np.stream_url:
            return
        # Reset the streaming-info throttle so the new track's first
        # audio-bitrate sample emits immediately (otherwise the
        # 2s throttle would block it if the previous track had
        # emitted within the last 2s).
        self._last_streaming_emit_t = 0.0
        # Drop the cached ICY title so the next station's first metadata
        # bump fires the bus signal even when it happens to match the
        # previous station's last value.
        self._last_radio_title = ""
        # Cast active? Route the new track to the receiver and skip
        # local mpv playback entirely. This makes "next track / album
        # auto-advance / queue play" go to the chromecast for free.
        if self._cast_active():
            cm = self._cast_manager
            dev = cm.active_cast
            # Reset poll-state tracking so the next status tick treats
            # this as a fresh track (avoids carrying over the previous
            # track's player_state and duration). The interpolation
            # anchor also resets so the progress bar starts at 0 and
            # ticks up as the new track plays, instead of continuing
            # from the previous track's final position.
            self._cast_last_player_state = None
            self._cast_last_duration_ms = -1
            self._cast_last_position_ms = -1
            self._cast_anchor_pos_ms = 0
            self._cast_anchor_wall = self._monotonic()
            # Route the new track to the armed cast device. The per-type
            # ladder (chromecast direct-play MIME pick + transcode fallback,
            # DLNA/Sonos off-thread SOAP push, AirPlay sync, Snapcast no-op)
            # lives in the unified CastManager.start_track — the same surface
            # the device-pick site (jellytoast.app._cast_to_device) calls. This
            # path is auto-advance, so no resume offset (start_track defaults
            # resume_seconds to 0.0, the chromecast async default).
            #
            # The chromecast attempt token guards against a stale callback
            # winning when the user presses Next again before the receiver
            # finishes negotiating; it was always chromecast-only (DLNA/Sonos
            # never bumped it), so the guard stays gated on the device type to
            # preserve that exact behaviour.
            is_cc = dev.device_type == CastType.CHROMECAST
            self._cast_attempt += 1
            token = self._cast_attempt

            def _on_cast_done(ok: bool, _np=np, _t=token, _is_cc=is_cc, _dev=dev) -> None:
                if _is_cc and _t != self._cast_attempt:
                    # A newer chromecast attempt is authoritative.
                    return
                if ok:
                    _np.is_paused = False
                    self.bus.playback_started.emit(_np)
                    self._begin_play_session(_np)
                    self._report_session_start(_np)
                else:
                    # Auto-advance cast push failed (receiver rejected the
                    # media, transcode 404, SOAP error, the device dropped off
                    # the network). Unlike the device-pick path (which restores
                    # local playback), auto-advance had NO fallback — active_cast
                    # stayed armed, the device sat idle, and the bar kept reading
                    # "Casting to <device>" with no way back to audio short of a
                    # manual disconnect. Tear the dead session down (mirrors
                    # stop()'s cast branch) so _on_cast_stopped hands the track
                    # back to local mpv (paused at position), and surface why.
                    logger.warning(
                        "cast auto-advance failed for %r on %s; tearing down "
                        "to local playback",
                        getattr(_np, "title", "?"),
                        getattr(_dev, "device_type", "?"),
                    )
                    try:
                        self._cast_manager.stop_cast()
                    except Exception:
                        pass
                    self.bus.cast_stopped.emit()
                    try:
                        from jellytoast import notifications

                        notifications.notify(
                            "Casting stopped",
                            "Couldn't play the next track on the cast device "
                            "— playback moved back to this computer.",
                            tag="jellytoast-cast",
                        )
                    except Exception:
                        pass

            cm.start_track(dev, np, provider=self.api, on_done=_on_cast_done)
            return
        if self._mpv is None:
            return

        # Auto-advance handoff: mpv's prefetch may have already started
        # this track gaplessly. If mpv is actively playing the URL we
        # prefetched and the item_id matches, re-issuing mpv.play()
        # would loadfile-replace it from zero (audible stutter). Match
        # by item_id rather than URL equality because Subsonic /
        # Navidrome rotate salt + token per get_audio_stream_url call
        # — the URL mpv is on (`_prefetched_url`, built when we queued
        # the track) and `np.stream_url` (built fresh when QueueManager
        # rebuilt the NowPlaying for the now-current track) differ as
        # strings even though they point at the same media.
        # Properties are accessed via attribute (`.path`); `[]` is
        # option lookup in python-mpv and "path"/"idle-active"/
        # "core-idle" are not options, so `["path"]` raises
        # "property does not exist".
        try:
            mpv_path = self._mpv.path
            idle_active = self._mpv.idle_active
            core_idle = self._mpv.core_idle
        except Exception:
            mpv_path = None
            idle_active = True
            core_idle = True
        is_handoff = (
            mpv_path is not None
            and not idle_active
            and not core_idle
            and self._prefetched_url is not None
            and mpv_path == self._prefetched_url
            and self._prefetched_item_id == np.item_id
        )
        if is_handoff:
            self._prefetched_url = None
            self._prefetched_item_id = None
            # Crossfade handoff completes here — clear the SWAP-residual
            # state so a future fade can arm cleanly.
            if self._crossfader is not None:
                self._crossfader.abort()
            np.is_paused = False
            self.bus.playback_started.emit(np)
            # Auto-advance via mpv's prefetched playlist entry. The
            # outgoing track ended naturally (mpv's playlist-pos
            # advanced) — close that session so the server records
            # the previous track as "watched in full" before we
            # report the new one as starting.
            self._begin_play_session(np)
            self._report_session_start(np)
            self._last_reported_position_ms = -1
            return

        # User-initiated play (next, jump, fresh queue) — kill any
        # active fade so the new track doesn't race the ramp.
        self._abort_crossfade()

        # Forward-skip onto the already-prefetched "next" track: advance
        # mpv's internal playlist to the appended entry (a gapless switch,
        # like a natural end-of-track) instead of mpv.play()'s
        # loadfile-replace. loadfile-replace closes + reopens the audio
        # output; on Windows/WASAPI that reinitializes the device and clicks
        # (the start-of-track buzz). A playlist advance keeps the output open.
        # Guarded to the case where this track IS the prefetched next, mpv has
        # it queued one past the current entry, it's audio, and there's no
        # resume offset (a fresh skip starts at 0). Session reporting uses the
        # same calls as the loadfile path below, so skip vs. scrobble semantics
        # are unchanged; on any miss we fall through to the reload path.
        skip_ms = max(0, int(getattr(np, "position", 0)))
        if (
            self._mpv is not None
            and getattr(np, "is_audio", True)
            and skip_ms <= 500
            and self._prefetched_item_id is not None
            and self._prefetched_item_id == np.item_id
            and self._prefetched_url is not None
        ):
            try:
                count = self._mpv.playlist_count
                pos = self._mpv.playlist_pos
                if count is not None and pos is not None and int(pos) + 1 < int(count):
                    self._mpv.command("playlist-next")
                    self._mpv["pause"] = False
                    self._prefetched_url = None
                    self._prefetched_item_id = None
                    np.is_paused = False
                    self.bus.playback_started.emit(np)
                    self._begin_play_session(np)
                    self._report_session_start(np)
                    self._last_reported_position_ms = -1
                    return
            except Exception as e:
                logger.warning("Gapless skip-next failed (%s); falling back to reload", e)

        try:
            # Different presentation for audio vs video
            if np.is_audio:
                self._mpv["force-window"] = "no"
                self._mpv["vid"] = "no"
            else:
                self._mpv["vid"] = "auto"
            # Resume support: if NowPlaying carries a non-trivial
            # position (set by the launch-time restore from
            # settings.saved_position_ms), seek there. Same deferred-
            # reset pattern as the cast disconnect handoff so mpv
            # consumes the offset before we clear it.
            start_ms = max(0, int(getattr(np, "position", 0)))
            start_sec = start_ms / 1000.0
            self._mpv["start"] = str(start_sec) if start_sec > 0.5 else "none"
            # mpv.play() is loadfile-replace — wipes any prefetched entry
            # we'd queued for the previous current track. State follows.
            self._prefetched_url = None
            self._prefetched_item_id = None
            self._mpv.play(np.stream_url)
            self._mpv["pause"] = False
            if start_sec > 0.5:
                QTimer.singleShot(750, self._reset_mpv_start)
            # Reset the model's pause flag BEFORE emitting — surfaces
            # render the play/pause glyph from np.is_paused inside their
            # playback_started handlers. Waiting for mpv's pause
            # observer to flip it is a race (and the observer's no-path
            # gate can swallow that fire entirely mid-load): pausing a
            # track then pressing Next left the bar showing "play"
            # while the next track audibly played.
            np.is_paused = False
            self.bus.playback_started.emit(np)
            self._begin_play_session(np)
            self._report_session_start(np)
            self._last_reported_position_ms = -1
        except Exception as e:
            logger.warning("Play error: %s", e)

    @Slot()
    def toggle_pause(self):
        if self._cast_active():
            self._cast_manager.cast_toggle_pause()
            return
        if self._mpv is None:
            return
        # Cold-launch resume: if mpv has nothing loaded but we have a
        # now-playing state (set by QueueManager's playback_restored
        # path), promote the toggle to a real play request so play()
        # honors np.position via mpv["start"]. Without this the press
        # is a no-op against an idle mpv. Path / idle-active are
        # properties not options, so they're read via attribute access.
        try:
            path = self._mpv.path
            idle_active = self._mpv.idle_active
        except Exception:
            path = None
            idle_active = True
        if not path or idle_active:
            np = get_now_playing()
            if np.item_id and np.stream_url:
                self.bus.play_requested.emit(np)
                return
        try:
            new_pause = not self._mpv.pause
            self._mpv.pause = new_pause
            # Mirror to the dormant sibling so a mid-fade pause freezes
            # both ramps. Crossfader's pause/resume methods own the
            # ramp-state freeze; the bool flip on the sibling is just
            # to keep its decoder in lockstep.
            if self._crossfader is not None:
                if new_pause:
                    self._crossfader.pause()
                else:
                    self._crossfader.resume()
        except Exception as e:
            logger.warning("toggle_pause failed: %s", e)

    @Slot()
    def stop(self):
        if self._cast_active():
            # Stop while casting: halt media on whatever device is active
            # by dispatching through stop_cast() (routes by device_type).
            # The old chromecast_stop() only halted Chromecast renderers
            # yet cleared active_cast regardless, so a DLNA/Sonos device
            # kept playing orphaned (and a device-switch double-played).
            self._cast_manager.stop_cast()
            # Mirror the disconnect paths (cast_dispatcher / cast_dialog):
            # cast_stopped is what restores local volume + reloads mpv at the
            # paused position (player_cast_transport), clears the "Casting to
            # <device>" label + _casting flag (now_playing_bar — _on_stopped
            # deliberately preserves that label while _casting is True), and
            # resets bus.cast_active (the bit-perfect/crossfade gate). Without
            # it, Stop-while-casting leaves all three stuck.
            self.bus.cast_stopped.emit()
            self.bus.playback_stopped.emit()
            return
        if self._mpv is None:
            return
        self._abort_crossfade()
        try:
            self._mpv.stop()
        except Exception:
            pass
        self._last_reported_position_ms = -1
        self._prefetched_url = None
        self._prefetched_item_id = None
        # User pressed Stop — close out the session with the matching
        # PlaySessionId so the server can attribute the partial play
        # rather than waiting for a 60s session timeout.
        self._end_play_session_if_active(force_finished=False)
        self.bus.playback_stopped.emit()

    # ── Crossfade ───────────────────────────────────────────────────────────

    def _ensure_crossfader(self) -> Optional[Crossfader]:
        """Lazy-instantiate the Crossfader on first need. Returns None
        when the user hasn't enabled crossfade in Settings, a cast is
        active (the research doc §2 calls this out: crossfade is
        local-playback only), or mpv isn't available."""
        if not self.settings.crossfade_enabled:
            return None
        if self._cast_active():
            return None
        # Raw ALSA (hw:) output is exclusive by nature — one open at a
        # time. A fade needs BOTH handles playing through the overlap,
        # so the sibling's open would fail mid-fade and kill the
        # incoming track. Route those users through the ordinary
        # gapless path instead (docs/research/audio_output_routing.md).
        if self.settings.audio_output_device.startswith("alsa/"):
            return None
        if self._mpv is None:
            return None
        if self._crossfader is None:
            self._crossfader = Crossfader(
                bus=self.bus,
                settings=self.settings,
                make_handle=self._make_mpv_handle,
                get_current_handle=lambda: self._mpv,
                get_current_item=lambda: get_now_playing().raw,
                is_casting=self._cast_active,
                swap_handles=self._swap_active_handle,
                parent=self,
            )
        return self._crossfader

    def _swap_active_handle(self, new_handle) -> None:
        """Crossfader-driven swap. The new handle is already playing the
        next track at the user's target volume; the old handle is the
        Crossfader's dormant sibling now.

        After the rotation we stamp the prefetch handoff fields so the
        QueueManager-driven ``play_requested`` (fired when we emit
        ``playback_ended`` below) routes through the existing handoff
        branch in ``play()`` and skips the second loadfile that would
        audibly re-start the track."""
        self._mpv = new_handle
        # The sibling was minted by _make_mpv_handle with no observers;
        # attach them now (idempotent + gated) so the newly-active handle
        # drives time-pos / duration / end-file. Without this the seek bar
        # freezes and the queue stalls at EOF after the first crossfade.
        self._attach_handle_observers(new_handle)
        try:
            self._prefetched_url = new_handle.path or ""
        except Exception:
            self._prefetched_url = ""
        # Item-id half of the handoff key: read off the Crossfader's
        # last ``queue_prefetch_request`` snapshot so ``play()``'s
        # handoff check matches both URL and item_id.
        next_np = self._crossfader.next_np if self._crossfader is not None else None
        self._prefetched_item_id = next_np.item_id if next_np is not None else None
        self._last_reported_position_ms = -1
        try:
            self._end_play_session_if_active(force_finished=True)
        except Exception:
            pass
        # Fire playback_ended so QueueManager advances its index. The
        # subsequent play_requested → play() will see prefetched_url
        # match and take the handoff path.
        self.bus.playback_ended.emit()

    def _local_target_volume(self) -> int:
        """The volume the active local handle *should* sit at right now,
        honouring the same three gates ``set_volume`` does: bit-perfect
        pins to 100, mute is modelled as 0 (with the real level stashed in
        ``_muted_volume``), otherwise the user's persisted ``settings.volume``
        — which ``set_volume`` keeps in sync even mid-drag."""
        if self.bus.bit_perfect_active:
            return 100
        if self._muted_volume is not None:
            return 0
        return int(max(0, min(100, self.settings.volume)))

    def _abort_crossfade(self) -> None:
        """Called from explicit-action paths (play / stop / seek) so the
        Crossfader doesn't fight a transition the user just commanded."""
        cf = self._crossfader
        if cf is None:
            return
        was_fading = cf.state == CrossfadeState.CROSSFADING
        cf.abort()
        # abort() deliberately leaves the active handle alone (the caller
        # owns it) — but a live fade has ramped that handle's volume DOWN,
        # and mpv's ``volume`` is a persistent player property that a bare
        # play()/loadfile does NOT reset. Without restoring it here, the
        # next track (and every later local track) would play at the
        # faded-down level until the user touched the slider. The natural-
        # EOF path is already covered by ``complete_now`` in ``_on_ended``;
        # this covers the explicit user Next / seek / stop paths.
        if was_fading and self._mpv is not None:
            try:
                self._mpv["volume"] = self._local_target_volume()
            except Exception:
                pass

    @Slot()
    def _on_crossfade_started(self):
        """When a crossfade arms, drop the OUTGOING handle's gapless
        prefetch entry. The next track is playing on the Crossfader's
        sibling; if the appended entry stays on the active handle, its
        real EOF mid-fade lets mpv gaplessly advance INTO the next track
        too — doubling/echoing it for the fade tail. The swap re-stamps
        the prefetch handoff fields afterwards, so the handoff still
        matches."""
        self._clear_prefetch()
        # The sibling handle was minted without the EQ chain — apply it now so
        # the incoming track fades in EQ'd instead of snapping the curve on at
        # the swap.
        self._apply_eq_to_sibling()

    # ── Gapless prefetch ────────────────────────────────────────────────────

    def _clear_prefetch(self):
        """Remove any playlist entries past the currently-playing one.
        Walks in reverse so removals don't shift the indices we still
        need to remove."""
        if self._mpv is None:
            return
        try:
            count = self._mpv.playlist_count
            pos = self._mpv.playlist_pos
        except Exception:
            self._prefetched_url = None
            self._prefetched_item_id = None
            return
        if count is None or pos is None:
            self._prefetched_url = None
            self._prefetched_item_id = None
            return
        for i in range(int(count) - 1, int(pos), -1):
            try:
                self._mpv.command("playlist-remove", str(i))
            except Exception:
                pass
        self._prefetched_url = None
        self._prefetched_item_id = None

    @Slot(object)
    def _on_prefetch_request(self, np):
        """Append the next track (or clear any pending prefetch).

        Routes through mpv's internal playlist so libmpv's
        prefetch-playlist=yes + gapless-audio=weak can do a true gapless
        switch when the current track ends — avoids the "stop, decode
        next, start" stutter you'd get from re-issuing mpv.play() on
        end-file. No-op when casting (mpv is dormant) or when mpv has
        no media loaded."""
        if self._mpv is None or self._cast_active():
            return
        # Drop any previously-queued prefetch first so we don't pile up
        # stale "next" candidates from a queue state that has since
        # changed (e.g. user toggled shuffle mid-track).
        self._clear_prefetch()
        if np is None or not np.stream_url:
            return
        # mpv must already be playing something for an append to be a
        # *prefetch*. If mpv is idle, appending would just queue two
        # cold starts back-to-back; the explicit play() path will
        # arrive next and re-emit prefetch_request once it's running.
        try:
            if self._mpv.idle_active:
                return
        except Exception:
            return
        # Don't prefetch the same URL we're already on (RepeatMode.ONE
        # case) — mpv would gaplessly transition into a re-play, which
        # is what the user wants, but we don't want a double entry in
        # the playlist. The end-file → next() → play() path handles
        # repeat-one explicitly.
        try:
            if self._mpv.path == np.stream_url:
                return
        except Exception:
            pass
        try:
            self._mpv.command("loadfile", np.stream_url, "append")
            self._prefetched_url = np.stream_url
            self._prefetched_item_id = np.item_id
        except Exception as e:
            logger.warning("Prefetch append failed: %s", e)

    @Slot(int)
    def seek(self, ms: int):
        if self._cast_active():
            self._cast_manager.cast_seek(ms / 1000.0)
            return
        if self._mpv is None:
            return
        # Seek during a fade: per research doc §7, cancel and let the
        # new position play out on the (newly active) instance. The full
        # 200ms fade-in polish lands with v2.
        self._abort_crossfade()
        try:
            self._mpv.seek(ms / 1000.0, reference="absolute")
        except Exception:
            pass

    @Slot(int)
    def seek_relative(self, ms: int):
        # Cast: best-effort relative seek, dispatched per backend (reads the
        # receiver's current position + absolute-seeks to pos±delta, so skip
        # works on DLNA/Sonos too, both directions).
        if self._cast_active():
            self._cast_manager.cast_seek_relative(ms / 1000.0)
            return
        if self._mpv is None:
            return
        try:
            self._mpv.seek(ms / 1000.0, reference="relative")
        except Exception:
            pass

    @Slot(int)
    def set_volume(self, vol: int):
        vol = max(0, min(100, vol))
        # Bit-perfect mode: any volume input path (slider, MPRIS,
        # keyboard hotkey, headphone-button media keys) is force-pinned
        # at 100 — software attenuation is a float multiply and not
        # bit-identical. See ``docs/bit_perfect.md`` and
        # ``docs/research/bit_perfect_playback.md`` §7 (T2). Visual lock
        # on the slider widget is a UX nicety; this guard is the actual
        # enforcement boundary.
        #
        # Gates on the *runtime* state (``bus.bit_perfect_active``)
        # rather than the raw setting flag: an MP3 stream with the
        # toggle checked has nothing to preserve, so locking the slider
        # would just frustrate the user. The runtime flag is False
        # there even if ``bit_perfect_mode`` is True.
        if self.bus.bit_perfect_active:
            vol = 100
        if self._cast_active():
            # Don't persist cast volume into settings.volume — that
            # field is the user's local-playback preference and getting
            # polluted by cast adjustments would silently shift their
            # baseline every time they disconnect. The cast device
            # remembers its own state via the receiver session. We DO
            # track it in _cast_volume so mute/unmute restores the level
            # the user was actually hearing (not the local baseline).
            self._cast_manager.cast_set_volume(vol)
            self._cast_volume = vol
            self._cast_volume_push_wall = self._monotonic()
            self.bus.volume_state.emit(vol)
            # Moving the slider above zero is the universal "unmute"
            # gesture — clear the saved mute level and the icon, same as
            # the local path below. (Cast returns early, so without this
            # a slider nudge while muted on cast would leave the icon lit
            # and _muted_volume stale.)
            if self._muted_volume is not None and vol > 0:
                self._muted_volume = None
                self.bus.mute_state.emit(False)
            return
        if self._mpv is None:
            return
        try:
            self._mpv["volume"] = vol
            self.settings.volume = vol
            # If a crossfade is mid-flight, retarget it too — otherwise the
            # next 50ms ramp tick overwrites this drag (it rescales the
            # arm-time snapshot) and the swap clamps the incoming handle to
            # the stale volume, losing the adjustment until the next track.
            # (getattr: the crossfader is lazily built; absent == None.)
            cf = getattr(self, "_crossfader", None)
            if cf is not None:
                cf.set_target_volume(vol)
            self.bus.volume_state.emit(vol)
            # Adjusting volume while muted is the universal "unmute"
            # gesture — drop the saved pre-mute volume so the slider
            # stays the source of truth, and clear the mute icon.
            if self._muted_volume is not None and vol > 0:
                self._muted_volume = None
                self.bus.mute_state.emit(False)
        except Exception:
            pass

    @Slot(bool)
    def set_audio_exclusive(self, on: bool):
        """Push the new exclusive-output state to the live mpv handle so
        it takes effect on the next track open. mpv applies
        ``audio-exclusive`` when the audio output is (re)opened, which
        happens on every ``play()`` — no track interruption needed.

        The Settings dialog has already persisted the bool; this slot
        is the runtime-apply side. ``_make_mpv_handle`` reads the same
        setting on subsequent handle constructions (e.g. crossfade
        sibling)."""
        if self._mpv is None:
            return
        try:
            self._mpv["audio-exclusive"] = "yes" if on else "no"
        except Exception as e:
            logger.warning("set_audio_exclusive failed: %s", e)

    @Slot(str)
    def set_audio_output_device(self, name: str):
        """Push the new output device to the live mpv handle(s); mpv
        applies ``audio-device`` when the audio output is (re)opened.
        The Settings dialog persisted the value already;
        ``_make_mpv_handle`` reads the same setting for future handle
        constructions. The crossfade sibling (if one exists) gets the
        same push so a fade doesn't come up on the old device.

        Two guards around the reopen (2026-06-11 live find — switching
        ALSA-direct → Auto mid-play left mpv with a DEAD audio output:
        it raced untimed through the internal gapless playlist, scrubber
        flying, no sound):
        - drop any prefetched playlist entry, so the next track arrives
          via the explicit play() path (clean AO close/open) instead of
          a gapless transition across the device change — mpv's gapless
          documented failure mode is 'audio disabled' when the AO can't
          reinitialize at the boundary;
        - schedule the audio-health watchdog, which recovers if the
          reopen still comes up dead (see _check_audio_health)."""
        name = (name or "auto").strip() or "auto"
        for handle in (
            self._mpv,
            self._crossfader.sibling if self._crossfader is not None else None,
        ):
            if handle is None:
                continue
            try:
                handle["audio-device"] = name
            except Exception as e:
                logger.warning("set_audio_output_device failed: %s", e)
        self._clear_prefetch()
        self._audio_health_stage = 0
        self._device_busy_notified = False
        self._bp_contested_notified = False
        self._schedule_audio_health_check()

    # ── Audio-health watchdog ─────────────────────────────────────────
    # mpv never stops on a dead audio output: it disables the audio
    # track and keeps "playing" untimed at decode speed. Detect the
    # zombie (file loaded + not paused + audio-params None) and recover
    # in stages instead of letting the queue machine-gun.

    _AUDIO_HEALTH_DELAY_MS = 3000

    def _schedule_audio_health_check(self):
        QTimer.singleShot(self._AUDIO_HEALTH_DELAY_MS, self._check_audio_health)

    def _probe_bit_perfect_contention(self):
        """Bit-perfect on the PipeWire path + OTHER apps' streams mixing
        on the sink = sound plays but the mixer is summing — the path
        isn't bit-perfect until the other playback stops. Detect via
        ``pactl list sink-inputs`` on a worker and toast once per
        episode (re-arms when a later check finds the mixer clean or
        the device changes)."""
        if getattr(self, "_bp_contested_notified", False):
            return
        if not bool(getattr(self.bus, "bit_perfect_active", False)):
            return
        if self.current_output_family() not in ("pipewire", "pulse"):
            return
        import shutil

        if not shutil.which("pactl"):
            return
        own_pid = os.getpid()

        def _count() -> int:
            import subprocess

            out = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return _foreign_sink_inputs(out.stdout or "", own_pid)

        def _on_result(n):
            if n:
                if not getattr(self, "_bp_contested_notified", False):
                    self._bp_contested_notified = True
                    logger.info(
                        "bit-perfect contested — %d other stream(s) mixing", n
                    )
                    self.bus.bit_perfect_contested.emit()
            else:
                self._bp_contested_notified = False

        from jellytoast.async_io import run_async

        run_async(_count, on_result=_on_result, on_error=lambda e: None)

    def _on_playback_error(self):
        """A track ended with mpv's ERROR reason — on this codebase that
        is almost always the audio output refusing to open (the file
        itself streamed fine moments ago). Shed the known killer
        (exclusive mode), then retry the CURRENT track once per error up
        to a cap, so a bad audio config pauses the music for a beat
        instead of killing it silently (2026-06-11: exclusive-on-PipeWire
        failed every open — even plain Auto playback was dead on
        arrival, and the construction-time fallback never sees it
        because mpv only opens the AO at first play)."""
        if self._cast_active():
            return
        n = getattr(self, "_consecutive_play_errors", 0) + 1
        self._consecutive_play_errors = n
        if n > 3:
            logger.error(
                "playback failed %d times in a row — giving up (check "
                "Settings → Playback → Audio output)",
                n,
            )
            return
        logger.warning("playback ended in error (attempt %d) — recovering", n)
        h = self._mpv
        device = (self.settings.audio_output_device or "auto").strip()
        if device.startswith("alsa/") and not bool(self.settings.audio_exclusive):
            # The user PINNED a direct device — never silently fall back
            # to the mixer (sound would come back, but shared and
            # mixed: the exclusivity they chose, betrayed without a
            # word). If the device is still enumerable, this failure is
            # almost certainly "another app holds it": surface the
            # device-busy dialog and stop. If it vanished (unplugged),
            # the documented fallback-to-Auto story applies.
            try:
                present = any(n_ == device for n_, _ in self.audio_device_choices())
            except Exception:
                present = True
            if present:
                if not getattr(self, "_device_busy_notified", False):
                    self._device_busy_notified = True
                    logger.warning(
                        "pinned ALSA device %s refused to open (in use by "
                        "another app?) — playback stopped",
                        device,
                    )
                    self.bus.audio_device_busy.emit(device)
                return
            logger.warning(
                "pinned ALSA device %s is gone (unplugged?) — falling back to Auto",
                device,
            )
            try:
                h["audio-device"] = "auto"
            except Exception:
                pass
        else:
            try:
                if bool(self.settings.audio_exclusive):
                    # The known PipeWire-killer. Shed it; the audio-health
                    # reconcile persists the setting off once audio is
                    # confirmed back.
                    self._audio_health_shed_exclusive = True
                    h["audio-exclusive"] = "no"
                elif n >= 2:
                    # Second strike without exclusive in play: fall back to
                    # Auto in case the pinned device is the problem.
                    h["audio-device"] = "auto"
            except Exception:
                pass
        from jellytoast.player_state import get_now_playing

        np = get_now_playing()
        if np is not None and np.stream_url:
            QTimer.singleShot(300, lambda: self.play(np))

    def _audio_is_zombie(self) -> bool:
        h = self._mpv
        if h is None or self._cast_active():
            return False
        try:
            if h.idle_active or h.pause:
                return False
            if h.time_pos is None:
                return False
            return h.audio_params is None
        except Exception:
            return False

    def _check_audio_health(self):
        """Staged recovery: (0) ao-reload with current settings;
        (1) shed to audio-device=auto + shared mode and reload —
        mirrors the construction-time layered fallback; (2) give up:
        pause so the user sees a stopped player instead of the queue
        racing silently. Each action re-arms the check; a healthy check
        resets the stage."""
        if not self._audio_is_zombie():
            self._audio_health_stage = 0
            try:
                # Audible audio confirmed → the error streak (if any) is
                # over; let future errors start a fresh recovery ladder,
                # and a future busy episode notify again.
                if self._mpv is not None and self._mpv.audio_params:
                    self._consecutive_play_errors = 0
                    self._device_busy_notified = False
            except Exception:
                pass
            # Playback is established and healthy — the moment to check
            # whether the bit-perfect claim is contested by other apps
            # mixing on the same sink.
            self._probe_bit_perfect_contention()
            if getattr(self, "_audio_health_shed_exclusive", False):
                # The stage-1 shed is what brought audio back — make the
                # divergence honest: persist exclusive OFF so the UI and
                # the next handle construction agree with the runtime
                # (2026-06-11: mpv's PipeWire exclusive mode failed every
                # open on a real box; the checkbox had silently broken
                # every PipeWire target).
                self._audio_health_shed_exclusive = False
                if bool(self.settings.audio_exclusive):
                    logger.warning(
                        "exclusive output failed on this audio server — "
                        "switched it off (audio recovered in shared mode)"
                    )
                    self.settings.audio_exclusive = False
                    try:
                        self.bus.audio_exclusive_changed.emit(False)
                    except Exception:
                        pass
            return
        h = self._mpv
        stage = getattr(self, "_audio_health_stage", 0)
        exclusive_on = bool(self.settings.audio_exclusive)
        if stage == 0 and not exclusive_on:
            # Exclusive isn't in play — a plain reload may be enough
            # (transient device hiccup).
            logger.warning("audio output dead while playing — ao-reload")
            try:
                h.command("ao-reload")
            except Exception:
                pass
            self._audio_health_stage = 1
            self._schedule_audio_health_check()
            return
        if stage <= 1:
            # Exclusive is the known PipeWire-killer (every open fails
            # with it on) — go straight to the shed rather than wasting
            # a cycle on ao-reload.
            logger.warning(
                "audio output dead — shedding to auto/shared and reloading"
            )
            try:
                h["audio-device"] = "auto"
                if exclusive_on:
                    self._audio_health_shed_exclusive = True
                h["audio-exclusive"] = "no"
                # mpv may have dropped the audio track entirely at a
                # failed gapless boundary — re-select it before the
                # reload so the reload has a track to bring up.
                try:
                    h["aid"] = "auto"
                except Exception:
                    pass
                h.command("ao-reload")
            except Exception:
                pass
            self._audio_health_stage = 2
            self._schedule_audio_health_check()
            return
        logger.error(
            "audio output unrecoverable — pausing playback (check Settings "
            "→ Playback → Audio output)"
        )
        try:
            h["pause"] = True
        except Exception:
            pass
        self._audio_health_stage = 0

    def current_output_family(self) -> str:
        """The audio-output family actually carrying sound right now
        (``pipewire`` / ``alsa`` / ``pulse`` / ``wasapi`` …), or '' when
        nothing is playing / unknown. This is mpv's ``current-ao`` — the
        resolved answer when the picker says Auto."""
        h = self._mpv
        if h is None:
            return ""
        try:
            return (h.current_ao or "").strip()
        except Exception:
            return ""

    def audio_device_choices(self) -> list:
        """mpv's ``audio-device-list`` as ``[(name, description), …]``
        for the Settings picker, CURATED down to real destinations —
        mpv's raw list is mostly noise (every ALSA PCM alias: rate
        converters, Speex DSP, up/downmix plugins, jack/oss/openal/sdl/
        sndio backend defaults, surround profile variants…; a real desktop
        showed 32 entries of which 7 were actual outputs). Set
        ``JT_AUDIO_DEVICES_ALL=1`` to see everything mpv enumerates.
        Empty when no live handle (Settings opened before first playback
        init) or on any mpv error — the picker then offers Auto plus the
        persisted value only."""
        if self._mpv is None:
            return []
        try:
            # MUST be attribute access: python-mpv's __getitem__ targets the
            # options/ namespace (options/audio-device-list does not exist),
            # so dict-style access raises and the picker would always fall
            # back to Auto-only. audio-device-list is a runtime property —
            # the attribute API is the property API.
            raw = self._mpv.audio_device_list or []
            out = []
            for d in raw:
                name = (d.get("name") or "").strip()
                if not name:
                    continue
                out.append((name, (d.get("description") or name).strip()))
            if os.environ.get("JT_AUDIO_DEVICES_ALL") == "1":
                return out
            curated = _curate_audio_devices(out)
            # Safety valve: if curation left nothing real (exotic box
            # whose only outputs are families we drop, e.g. pure JACK),
            # the raw list is more useful than an empty picker.
            return curated if len(curated) > 1 else out
        except Exception as e:
            logger.warning("audio-device-list read failed: %s", e)
            return []

    @Slot(str)
    def set_replaygain(self, mode: str):
        if mode not in ("no", "track", "album"):
            return
        self.settings.replaygain = mode
        if self._mpv is None:
            return
        try:
            self._mpv["replaygain"] = mode
        except Exception:
            pass

    @Slot(bool, list)
    def apply_eq(self, enabled: bool, bands: list) -> None:
        """Build and assign the mpv ``af`` audio-filter chain for the
        EQ. Wired to ``PlayerBus.eq_changed`` (the user-facing path:
        slider release / preset pick / enabled toggle) and re-fired
        at the head of every track via ``_reapply_eq_on_start`` so a
        sample-rate change that re-plugs mpv's output doesn't drop
        the curve.

        Contract:

        * Called with ``enabled=False`` → filter is cleared. Stale
          band state isn't kept resident in mpv — the user toggling
          off should mean off.
        * Called with ``enabled=True`` and a 10-band list → writes
          a ``volume=<preamp>,anequalizer=...`` chain to ``self._mpv["af"]``.
          Pre-amp is read from settings (not the signal — keeps the
          signal payload shape stable when the pre-amp slider moves).
        * Wrong-length / non-numeric bands → log + skip rather than
          crash. The settings property normalizes shape, so this is
          a defence-in-depth check.
        * Idempotent — repeated calls with the same (enabled, bands,
          preamp) tuple after the last successful write are a no-op
          (``_last_eq_state``).
        """
        from jellytoast.eq_presets import BAND_COUNT

        enabled = bool(enabled)
        # Normalise the bands list to a tuple of floats so the
        # idempotence comparison doesn't fight list-vs-tuple or
        # mixed numeric types. A bad entry collapses to 0.0 here so
        # we always have a comparable shape, even on the disabled
        # path where the formatter won't run.
        try:
            normalised = tuple(float(b) for b in (bands or []))
        except (TypeError, ValueError):
            logger.warning("apply_eq: non-numeric bands, skipping")
            return
        if enabled and len(normalised) != BAND_COUNT:
            logger.warning(
                "apply_eq: expected %s bands, got %s — skipping",
                BAND_COUNT,
                len(normalised),
            )
            return

        # Pre-amp is part of the cache key so a slider drag on it
        # alone re-writes the chain. Settings clamps the value to
        # ±12 dB; defensive fallback to 0.0 here keeps the chain
        # safe even if Settings ever returns a surprise.
        try:
            preamp = float(self.settings.eq_preamp)
        except Exception:
            preamp = 0.0

        # Linear-phase FIR mode (EQ T2) — included in the cache key
        # so toggling the mode invalidates the cached short-circuit and
        # forces apply_eq to rewrite the chain.
        try:
            linear_phase = bool(self.settings.eq_linear_phase)
        except Exception:
            linear_phase = False

        # AutoEQ headphone-correction profile (EQ T3a). When populated,
        # parsed bands replace the 10-band graphic gains; the profile's
        # own pre-amp is added to the user's master pre-amp. The raw
        # JSON string is the cache key — content-addresses the active
        # profile so clear / reimport reliably trigger a re-apply.
        try:
            autoeq_json = str(self.settings.eq_autoeq_profile_json or "")
        except Exception:
            autoeq_json = ""

        new_state = (enabled, normalised, preamp, linear_phase, autoeq_json)
        if new_state == self._last_eq_state:
            return

        if self._mpv is None:
            self._last_eq_state = new_state
            return

        # Build + assign the chain via the shared helper (also used for the
        # crossfade sibling so a fade-in track isn't EQ-less). Channel count
        # comes from the active handle's live audio params (stereo fallback).
        try:
            self._mpv["af"] = self._eq_af_chain(new_state, self._eq_channel_count(self._mpv))
        except Exception as e:
            logger.warning("apply_eq failed: %s", e)
            return
        self._last_eq_state = new_state

    def _eq_channel_count(self, handle) -> int:
        """mpv's live output channel count for the ``af`` cross-product, or 2
        (stereo) when not yet readable. ``anequalizer`` needs concrete channel
        indices (the eq_dsp_v2 §2 'wart')."""
        if handle is None:
            return 2
        try:
            # Attribute access, NOT handle["…"]: python-mpv's __getitem__
            # targets options/, and audio-params is a pure runtime
            # property — dict access always raises, which silently pinned
            # every EQ chain to stereo (same class as the 2026-06-11
            # audio-device-list picker bug).
            params = handle.audio_params or {}
            raw = params.get("channel-count")
            if raw is not None:
                return int(raw)
        except Exception:
            pass
        return 2

    def _eq_af_chain(self, state: tuple, channel_count: int) -> str:
        """Build the mpv ``af`` filter-chain string for an EQ state tuple
        ``(enabled, bands, preamp, linear_phase, autoeq_json)`` at the given
        channel count. ``""`` clears the chain (disabled / malformed). Shared
        by ``apply_eq`` (active handle) and ``_apply_eq_to_sibling`` (the
        crossfade incoming handle) so the two stay byte-identical.

        Chain: ``volume=<preamp>dB`` (dropped at 0 dB) → anequalizer / fir-
        equalizer. Pre-amp first (eq_dsp.md §3) for boost headroom; an AutoEQ
        profile overrides the graphic bands and adds its own pre-amp."""
        from jellytoast.eq_presets import (
            BAND_COUNT,
            format_anequalizer_parametric,
            format_anequalizer_string,
            format_firequalizer_parametric,
            format_firequalizer_string,
        )

        enabled, normalised, preamp, linear_phase, autoeq_json = state
        if not enabled or len(normalised) != BAND_COUNT:
            return ""
        autoeq_bands: list = []
        autoeq_preamp_db = 0.0
        if autoeq_json:
            try:
                parsed = json.loads(autoeq_json)
                autoeq_bands = list(parsed.get("bands", []))
                autoeq_preamp_db = float(parsed.get("preamp_db", 0.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                autoeq_bands = []
                autoeq_preamp_db = 0.0
        if linear_phase:
            # EQ T2 — linear-phase FIR; global across channels (no cross-product).
            if autoeq_bands:
                chain = format_firequalizer_parametric(autoeq_bands)
            else:
                chain = format_firequalizer_string(list(normalised))
        else:
            if autoeq_bands:
                chain = format_anequalizer_parametric(autoeq_bands, channel_count=channel_count)
            else:
                chain = format_anequalizer_string(list(normalised), channel_count=channel_count)
        effective_preamp = preamp + autoeq_preamp_db
        if abs(effective_preamp) > 1e-9:
            if float(effective_preamp).is_integer():
                p_str = str(int(effective_preamp))
            else:
                p_str = f"{effective_preamp:g}"
            chain = f"volume={p_str}dB," + chain
        return chain

    def _apply_eq_to_sibling(self) -> None:
        """Stamp the current EQ chain onto the Crossfader's incoming (sibling)
        handle, so a cross-faded-in track isn't EQ-less for the whole fade
        (after the swap, ``_reapply_eq_on_start`` covers it). Best-effort:
        channel count falls back to stereo before the sibling's audio params
        stabilise, and an mpv error is swallowed — worst case is that one fade
        plays un-EQ'd (the prior behaviour), never a crash."""
        cf = getattr(self, "_crossfader", None)
        sib = cf.sibling if cf is not None else None
        state = self._last_eq_state
        if sib is None or not state or not state[0]:
            return
        try:
            sib["af"] = self._eq_af_chain(state, self._eq_channel_count(sib))
        except Exception as e:
            logger.warning("apply EQ to crossfade sibling failed: %s", e)

    @Slot(object)
    def _reapply_eq_on_start(self, _np) -> None:
        """Read the persisted EQ state and re-assert it on the new
        track. No-op when the chain matches what's already on mpv —
        ``apply_eq`` short-circuits on ``_last_eq_state``."""
        try:
            enabled = self.settings.eq_enabled
            bands = self.settings.eq_bands
        except Exception:
            return
        # Force a re-write even when the state matches our last
        # cache: the playback_started edge is precisely the case
        # where mpv may have dropped the filter graph during a
        # gapless re-plug, so the cache is stale. Clearing
        # ``_last_eq_state`` makes apply_eq fall through.
        self._last_eq_state = None
        self.apply_eq(enabled, bands)

    @Slot()
    def toggle_mute(self):
        """Mute via volume save/restore — never touches mpv["mute"]
        (and so never sets the PipeWire stream node's mute flag).

        WirePlumber's restore-stream persists per-app PW mute and re-
        applies it on every launch, so a single accidental PW-node
        mute would leave jellytoast silently muted across all future
        sessions. Driving mute as volume=0 keeps the node's mute flag
        permanently false and the persistence trap closed."""
        if self._cast_active():
            # While casting, mpv is idle, so the local volume=0 trick is a
            # silent no-op AND desyncs the mute icon. Route mute through
            # the receiver, mirroring set_volume's cast branch. Save &
            # restore the LIVE cast level (_cast_volume), not the local
            # settings.volume baseline — the two diverge (cast starts at
            # _CAST_INITIAL_VOLUME and tracks the slider independently),
            # so restoring the baseline would slam the speaker to ~100% on
            # unmute. (_muted_volume cleared too by _on_cast_stopped.)
            try:
                # Stamp the suppression window so the poll's receiver-sync
                # doesn't mistake the device's lagging echo of this write
                # (especially the unmute restore) for an external change.
                self._cast_volume_push_wall = self._monotonic()
                if self._muted_volume is None:
                    self._muted_volume = int(self._cast_volume)
                    self._cast_manager.cast_set_volume(0)
                    self.bus.mute_state.emit(True)
                else:
                    restored = self._muted_volume
                    self._muted_volume = None
                    self._cast_manager.cast_set_volume(restored)
                    self.bus.mute_state.emit(False)
            except Exception:
                pass
            return
        if self._mpv is None:
            return
        try:
            if self._muted_volume is None:
                # Muting: stash the live volume, drop to zero.
                self._muted_volume = int(self._mpv["volume"])
                self._mpv["volume"] = 0
                self.bus.mute_state.emit(True)
            else:
                # Unmuting: restore the stashed volume.
                restored = self._muted_volume
                self._muted_volume = None
                self._mpv["volume"] = restored
                self.bus.mute_state.emit(False)
        except Exception:
            pass

    # ── Sleep timer ─────────────────────────────────────────────────────────

    _SLEEP_FIRE_MODES = ("pause", "end_of_track", "fade_stop")

    def start_sleep_timer(self, seconds: int, on_fire: str = "pause") -> None:
        """Arm a one-shot countdown. Replacing an active timer is
        idempotent: the previous timer is cancelled first so we don't
        leak Qt timers or fire twice."""
        if on_fire not in self._SLEEP_FIRE_MODES:
            on_fire = "pause"
        seconds = max(0, int(seconds))
        self.cancel_sleep_timer()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(self._on_sleep_timer_elapsed)
        self._sleep_timer = t
        self._sleep_on_fire = on_fire
        self._sleep_pending_eot = False
        t.start(seconds * 1000)
        self.bus.sleep_timer_started.emit(seconds)

    def cancel_sleep_timer(self) -> None:
        had_fade = self._sleep_fade_timer is not None
        if had_fade:
            # Restore the user's original volume before tearing the ramp
            # down — otherwise their next play session starts at whatever
            # mid-fade value we'd written into mpv.
            self._cancel_sleep_fade(restore_volume=True)
        if self._sleep_timer is None and not self._sleep_pending_eot and not had_fade:
            return
        had_timer = self._sleep_timer is not None or self._sleep_pending_eot or had_fade
        if self._sleep_timer is not None:
            try:
                self._sleep_timer.stop()
            except Exception:
                pass
            self._sleep_timer = None
        self._sleep_pending_eot = False
        if had_timer:
            self.bus.sleep_timer_cancelled.emit()

    @Slot()
    def _on_sleep_timer_elapsed(self):
        mode = self._sleep_on_fire
        self._sleep_timer = None
        if mode == "end_of_track":
            # Defer the pause until the current track finishes — the
            # `playback_ended` handler reads this flag.
            self._sleep_pending_eot = True
            self.bus.sleep_timer_fired.emit()
            return
        self.bus.sleep_timer_fired.emit()
        if mode == "fade_stop":
            # Cast path: fading mpv's volume wouldn't affect what the
            # receiver is actually playing. Out of scope for this branch.
            if self._cast_active():
                self.pause()
                return
            self._fade_volume_to_zero_then_pause(self.settings.sleep_fade_duration_ms)
            return
        self.pause()

    # Tick interval for the linear fade ramp. 50ms = 20Hz, smooth
    # enough that the ear hears a continuous slide rather than steps.
    _SLEEP_FADE_TICK_MS = 50

    def _fade_volume_to_zero_then_pause(self, duration_ms: int) -> None:
        """Legacy mid-track fade path — kept as a thin wrapper around
        ``_fade_volume_to_zero`` so older call-sites still work even
        though the X-minute presets now use the "fade-at-end-of-track"
        flow via ``_arm_sleep_eot_fade_if_due``."""
        self._fade_volume_to_zero(duration_ms, pause_on_complete=True)

    def _fade_volume_to_zero(
        self, duration_ms: int, *, pause_on_complete: bool = True
    ) -> None:
        """Linearly ramp mpv volume to zero over ``duration_ms``.

        ``pause_on_complete=True`` (the legacy behaviour): pause + restore
        the original volume when the ramp hits zero. Used by the
        immediate fade-to-stop path.

        ``pause_on_complete=False``: leave the ramp at zero; the natural
        ``playback_ended`` → EOT pause handler restores the volume.
        Used by the "current song fades out at its end" flow."""
        if self._mpv is None:
            if pause_on_complete:
                self.pause()
            return
        try:
            current = float(self._mpv["volume"] or 0)
        except Exception:
            if pause_on_complete:
                self.pause()
            return
        duration_ms = max(self._SLEEP_FADE_TICK_MS, int(duration_ms))
        steps = max(1, duration_ms // self._SLEEP_FADE_TICK_MS)
        self._sleep_fade_original_volume = int(round(current))
        self._sleep_fade_current_volume = current
        self._sleep_fade_steps_remaining = steps
        self._sleep_fade_step_decrement = current / steps if steps else current
        self._sleep_fade_pause_on_complete = bool(pause_on_complete)
        t = QTimer(self)
        t.setInterval(self._SLEEP_FADE_TICK_MS)
        t.timeout.connect(self._on_sleep_fade_tick)
        self._sleep_fade_timer = t
        t.start()

    @Slot()
    def _on_sleep_fade_tick(self) -> None:
        if self._sleep_fade_timer is None or self._mpv is None:
            return
        self._sleep_fade_steps_remaining -= 1
        if self._sleep_fade_steps_remaining <= 0:
            try:
                self._mpv["volume"] = 0
            except Exception:
                pass
            if self._sleep_fade_pause_on_complete:
                self._finish_sleep_fade_and_pause()
            else:
                # Ramp done; the playback_ended → EOT handler will pause
                # + restore the original volume. We keep
                # ``_sleep_fade_original_volume`` populated for that
                # handler to consume, but tear the timer down so we
                # don't keep ticking on the silent tail of the track.
                t = self._sleep_fade_timer
                self._sleep_fade_timer = None
                if t is not None:
                    try:
                        t.stop()
                    except Exception:
                        pass
                    try:
                        t.deleteLater()
                    except Exception:
                        pass
                self._sleep_fade_steps_remaining = 0
                self._sleep_fade_step_decrement = 0.0
                self._sleep_fade_current_volume = 0.0
            return
        self._sleep_fade_current_volume = max(
            0.0,
            self._sleep_fade_current_volume - self._sleep_fade_step_decrement,
        )
        try:
            self._mpv["volume"] = int(round(self._sleep_fade_current_volume))
        except Exception:
            pass

    def _finish_sleep_fade_and_pause(self) -> None:
        original = self._sleep_fade_original_volume
        self._cancel_sleep_fade(restore_volume=False)
        self.pause()
        # Restore the user's volume so the next play session isn't silent.
        if original is not None and self._mpv is not None:
            try:
                self._mpv["volume"] = original
            except Exception:
                pass

    def _cancel_sleep_fade(self, *, restore_volume: bool) -> None:
        t = self._sleep_fade_timer
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
            try:
                t.deleteLater()
            except Exception:
                pass
        self._sleep_fade_timer = None
        original = self._sleep_fade_original_volume
        self._sleep_fade_original_volume = None
        self._sleep_fade_steps_remaining = 0
        self._sleep_fade_step_decrement = 0.0
        self._sleep_fade_current_volume = 0.0
        if restore_volume and original is not None and self._mpv is not None:
            try:
                self._mpv["volume"] = original
            except Exception:
                pass

    @Slot(object)
    def _on_track_started_sleep_fade_reset(self, _np) -> None:
        # No fade running → nothing to do.
        if self._sleep_fade_timer is None:
            return
        # The skip / queue-advance broke the assumption that the fade was
        # decaying toward THIS track's end. Restore the user's volume so
        # the new track starts at full level; if EOT is still pending,
        # ``_arm_sleep_eot_fade_if_due`` will re-arm the fade on the new
        # track's last N seconds.
        self._cancel_sleep_fade(restore_volume=True)

    @Slot()
    def _on_sleep_eot_check(self):
        if not self._sleep_pending_eot:
            return
        self._sleep_pending_eot = False
        # Restore the original volume the fade-at-track-end may have
        # decayed to zero. ``_sleep_fade_original_volume`` is stashed by
        # ``_fade_volume_to_zero`` and intentionally left populated when
        # the fade completes with ``pause_on_complete=False`` so we can
        # consume it here. None means no fade ran — leave volume alone.
        original = self._sleep_fade_original_volume
        self._sleep_fade_original_volume = None
        if original is not None and self._mpv is not None:
            try:
                self._mpv["volume"] = original
            except Exception:
                pass
        self.pause()

    def pause(self) -> None:
        """Idempotent pause — used by the sleep-timer fire paths. Routes
        through `toggle_pause` when mpv is actually playing so the cast
        branch + cold-launch promotion stay one code path."""
        if self._cast_active():
            try:
                self._cast_manager.cast_toggle_pause()
            except Exception:
                pass
            return
        if self._mpv is None:
            return
        try:
            if not self._mpv.pause:
                self._mpv.pause = True
        except Exception:
            pass

    # ── Property observer slots (run on Qt thread) ──────────────────────────

    PROGRESS_REPORT_DELTA_MS = 5_000

    @Slot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        np.position = ms
        self.bus.position_updated.emit(ms)
        # Forward to Jellyfin only when the head has moved a noticeable
        # chunk — and reset the anchor on backwards jumps (seek) so the
        # next forward step still gates correctly. Persist resume
        # position on the same gate so disk writes stay 5s-paced.
        if (
            self._last_reported_position_ms < 0
            or abs(ms - self._last_reported_position_ms) >= self.PROGRESS_REPORT_DELTA_MS
        ):
            self._last_reported_position_ms = ms
            self._report_progress()
            if np.item_id:
                self.settings.saved_position_ms = ms
                self.settings.saved_position_item_id = np.item_id
        cf = self._ensure_crossfader()
        # When an end-of-track sleep timer is pending, the current track must
        # play out and the queue stop — NOT crossfade into the next track.
        # Letting both arm at the boundary started the next track on the
        # sibling, swapped it in, then paused it at full volume (and the EOT
        # volume-restore landed on the wrong, new handle). Gate the crossfade
        # so the sleep-EOT fade below tapers the current track and it ends
        # naturally. (An already-running fade isn't aborted — on_position is a
        # no-op once CROSSFADING — which is fine: the timer fired mid-swap.)
        if cf is not None and not self._sleep_pending_eot:
            duration_ms = int(np.duration or self._current_duration_ms or 0)
            cf.on_position(ms, duration_ms)
        self._arm_sleep_eot_fade_if_due(ms, np)

    def _arm_sleep_eot_fade_if_due(self, ms: int, np) -> None:
        """When the sleep timer has fired and we're waiting for the
        current track to end (``_sleep_pending_eot``), start a volume
        fade as soon as we enter the last ``sleep_fade_duration_ms`` of
        the track so the song tapers off instead of cutting hard. No-op
        under bit-perfect (the lock forbids volume manipulation; the
        track just plays out at full volume), while casting (the local
        mpv volume isn't what's reaching the receiver), or if a fade is
        already running.
        """
        if not self._sleep_pending_eot:
            return
        if self._sleep_fade_timer is not None:
            return
        if self.bus.bit_perfect_active:
            return
        if self._cast_active():
            return
        duration_ms = int(getattr(np, "duration", 0) or self._current_duration_ms or 0)
        if duration_ms <= 0:
            return
        try:
            fade_window_ms = int(self.settings.sleep_fade_duration_ms)
        except Exception:
            return
        if fade_window_ms <= 0:
            return
        remaining_ms = duration_ms - ms
        if remaining_ms <= 0 or remaining_ms > fade_window_ms:
            return
        # Ramp over the actual time left so the fade ends right around
        # the natural track end. ``pause_on_complete=False`` — the
        # playback_ended → EOT path owns the pause + volume restore.
        self._fade_volume_to_zero(remaining_ms, pause_on_complete=False)

    @Slot(int)
    def _on_duration(self, ms: int):
        np = get_now_playing()
        if ms > 0 and np.duration == 0:
            np.duration = ms
        if ms > 0:
            self._current_duration_ms = ms
        self.bus.duration_set.emit(ms)

    @Slot(bool)
    def _on_paused(self, paused: bool):
        np = get_now_playing()
        np.is_paused = paused
        # The pause property observer fires once on registration with
        # mpv's default pause=False — even when mpv has nothing loaded.
        # Without this gate, that initial fire emits playback_resumed
        # at boot and clobbers the resume icon (which should read as
        # "play / paused at saved position"). Gate on `path` so we only
        # forward state changes when a file is actually loaded — UNLESS
        # a cast is active: then mpv is idle (no path) but the cast
        # status feed is the authoritative pause source and must be
        # forwarded, or the transport button never flips while casting.
        if not self._cast_active():
            try:
                path = self._mpv.path if self._mpv is not None else None
            except Exception:
                path = None
            if not path:
                return
        if paused:
            self.bus.playback_paused.emit()
        else:
            self.bus.playback_resumed.emit()
        # Server needs to learn about pause/resume immediately so its
        # "now playing" UI flips state without waiting for a position
        # delta — force one report through.
        if np.item_id:
            self._report_progress()

    @Slot()
    def _on_ended(self):
        # If a crossfade is mid-ramp, the outgoing track hitting its real
        # EOF is EXPECTED — the sibling is already playing the next track.
        # Complete the fade (jump the sibling to full volume + swap) rather
        # than letting the normal advance→play() path abort the fade and
        # restart the next track on the faded-DOWN outgoing handle (which
        # leaves it near-silent). complete_now()→_swap_active_handle still
        # emits playback_ended, so the queue advances + play() takes the
        # handoff path — no double advance.
        cf = self._crossfader
        if cf is not None and cf.state == CrossfadeState.CROSSFADING:
            cf.complete_now()
            return
        # Track played to completion. Closing the session out here
        # (with PositionTicks at the track's full duration) tells the
        # server "this finished" — Jellyfin's auto-played threshold
        # marks the item watched off the Stopped report, so we don't
        # need a separate mark_played call. Skipping the previous
        # explicit mark_played also avoids the redundant POST that
        # the audit flagged.
        self._end_play_session_if_active(force_finished=True)
        self._last_reported_position_ms = -1
        self.bus.playback_ended.emit()

    def _on_streaming_info(self, codec: str, kbps: int):
        """Re-emit mpv's audio-bitrate / codec to the bus on the Qt
        thread. The transport bar listens to populate its optional
        "Streaming X · Y kbps" indicator."""
        self._current_codec = codec or ""
        self._refresh_bit_perfect_active()
        self.bus.streaming_info_updated.emit(codec, kbps)

    # Codecs whose bitstream survives untouched through the pipeline.
    # PCM / DSD variants are matched by prefix. Lossy codecs are
    # deliberately omitted — a lossy source can't be "bit perfect"
    # regardless of which flags the user has flipped.
    _LOSSLESS_CODECS = frozenset({
        "flac", "alac", "wavpack", "ape", "tta", "shorten",
        "mlp", "truehd", "dsd", "wav",
    })

    @classmethod
    def _is_lossless_codec(cls, codec: str) -> bool:
        c = (codec or "").strip().lower()
        if not c:
            return False
        if c in cls._LOSSLESS_CODECS:
            return True
        return c.startswith("pcm_") or c.startswith("dsd_")

    def _compute_bit_perfect_active(self) -> bool:
        """The runtime "is the bit-perfect contract actually in force?"
        check. Setting on + no server transcode + lossless source +
        not currently casting (the cast pipeline isn't local mpv, so
        the contract is meaningless there)."""
        if self._cast_active_flag:
            return False
        if not self.settings.bit_perfect_mode:
            return False
        # Empty normalizes to "original" (direct-play) — same whole-class
        # sweep as _resolve_play_method / get_audio_stream_url, so bit-perfect
        # engages on a lossless direct stream instead of silently staying off.
        if (self.settings.audio_quality or "original").strip().lower() != "original":
            return False
        return self._is_lossless_codec(self._current_codec)

    def _refresh_bit_perfect_active(self):
        """Recompute the runtime state and broadcast if it changed.
        Called whenever a contributing input flips: codec arrival,
        setting toggle, audio-quality change, cast start/stop.

        False→True transitions also reconcile two side-effects of the
        lock taking hold: any in-flight sleep-timer fade is cancelled
        (it would otherwise silently no-op under the volume clamp),
        and mpv's volume is pushed to 100 so the cast→stop edge can't
        leave a stale lower value under the new contract."""
        new = self._compute_bit_perfect_active()
        prev = self.bus.bit_perfect_active
        if new == prev:
            return
        self.bus.bit_perfect_active = new
        self.bus.bit_perfect_active_changed.emit(new)
        if new and not prev:
            # Lock just took hold. Stop any volume ramp first so the
            # fade timer doesn't race the clamp. ``restore_volume=False``
            # because we're about to overwrite it with 100 anyway.
            if self._sleep_fade_timer is not None:
                self._cancel_sleep_fade(restore_volume=False)
            # set_volume re-routes through the same lock guard, so the
            # final write is always 100; call it for the side-effects
            # (mpv["volume"] write, settings persist, slider notify).
            self.set_volume(100)

    @Slot(bool)
    def _on_bit_perfect_setting_changed(self, _enabled: bool):
        """Re-evaluate the runtime state whenever the user flips the
        master setting (the codec hasn't changed, but the gate did)."""
        self._refresh_bit_perfect_active()

    @Slot(str)
    def _on_audio_quality_changed(self, quality: str):
        """Re-evaluate when the user picks a different audio_quality
        tier — switching off "original" means the next stream will be
        server-transcoded, breaking the bit-perfect contract before
        the new codec even arrives.

        Also push the *effective* exclusive-output state to the live
        mpv handle: exclusive requires the full bit-perfect contract,
        and a transcoded tier disqualifies it even when the setting
        is still on. Without this, a user could leave exclusive
        enabled, flip quality from "original" to "low" mid-session,
        and the next track would silently re-open the audio output
        in exclusive mode despite the contract being broken."""
        self._refresh_bit_perfect_active()
        effective_exclusive = (
            bool(self.settings.bit_perfect_mode)
            and bool(self.settings.audio_exclusive)
            and (quality or "").strip().lower() == "original"
        )
        self.set_audio_exclusive(effective_exclusive)

    @Slot()
    def _on_cast_started_bit_perfect(self):
        """Casting → the local mpv pipeline goes idle, so the runtime
        bit-perfect contract can't be in force. Drop the flag (the
        slider unlocks, the badge clears) and forget the cached codec
        so a cast_stopped doesn't restore a stale state from before
        the cast.

        Also mirrors onto ``bus.cast_active`` so call-sites without
        a cast-signal connection (Settings → Bit-perfect's vol=100
        push, for one) can read the live cast state synchronously.

        Cancels any in-flight sleep-timer fade — it would otherwise
        keep ramping local mpv's volume (which has no listener) while
        the cast device plays on, oblivious to the timer. The fade
        timer fires on local volume that doesn't reach the cast
        receiver, so finishing the ramp wouldn't lower the cast
        anyway. ``restore_volume=True`` so when the user resumes
        local playback, mpv is back at the pre-fade level — they
        cancelled the cast, they didn't ask for silence."""
        self._cast_active_flag = True
        self.bus.cast_active = True
        self._current_codec = ""
        if self._sleep_fade_timer is not None:
            self._cancel_sleep_fade(restore_volume=True)
        self._refresh_bit_perfect_active()

    @Slot()
    def _on_cast_stopped_bit_perfect(self):
        """Cast ended → mpv resumes. The flag will rise back to True
        once the next codec report arrives (and matches the lossless
        criteria); meantime stay False."""
        self._cast_active_flag = False
        self.bus.cast_active = False
        self._refresh_bit_perfect_active()

    def _on_radio_title(self, title: str):
        """Re-emit mpv's ICY title to the bus on the Qt thread. The
        now-playing surfaces listen while the queue context is
        INTERNET_RADIO to swap the static track title for the live
        feed's metadata."""
        self.bus.radio_title_changed.emit(title)

    # ── Server progress reporting ───────────────────────────────────────────

    def _report_progress(self):
        np = get_now_playing()
        if not np.item_id:
            return
        # Fire-and-forget on the pool — this fires on a timer, so a
        # blocking provider call to a dead server would stutter the GUI
        # every tick. See _report_session_start.
        iid, pos, paused = np.item_id, np.position_ticks, np.is_paused
        sid, pm = self._session_id, self._session_play_method
        run_async(
            lambda: self.api.report_playback_progress(
                iid, pos, paused, play_session_id=sid, play_method=pm
            ),
            on_error=lambda _e: None,
        )

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self):
        if self._crossfader is not None:
            try:
                self._crossfader.shutdown()
            except Exception:
                pass
        if self._mpv is not None:
            # Close the active play session (if any) so the server
            # records the final position rather than session-timeout.
            try:
                self._end_play_session_if_active(force_finished=False)
            except Exception:
                pass
            try:
                self._mpv.terminate()
            except Exception:
                pass
