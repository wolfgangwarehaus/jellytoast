"""
mpv-based playback backend.

Why mpv:
- Bit-perfect audio (FLAC, ALAC, OPUS, DSD) — no transcoding
- Gapless playback for albums
- ReplayGain support
- Lower CPU/RAM than browser-based playback
- Native to Linux/Arch

This module exposes:
- MpvController (headless audio + signal wiring)
"""

import time
import uuid
from typing import Optional
from PySide6.QtCore import QObject, QTimer, Slot, Signal

try:
    import mpv

    MPV_AVAILABLE = True
except (ImportError, OSError) as e:
    MPV_AVAILABLE = False
    _MPV_ERROR = str(e)

from modules.player_state import PlayerBus, NowPlaying, get_now_playing
from modules.settings import get_settings
from modules.providers import get_provider
from modules.async_io import run_async
from modules.playback.crossfade import Crossfader, crossfade_env_enabled


class _CastStatusSignal(QObject):
    """Pure signal carrier — pychromecast fires status callbacks on its
    own worker thread, and emitting a Qt signal there hands off to the
    receiver's thread (queued connection) automatically. We can't rely
    on QTimer.singleShot from a non-Qt thread because it wouldn't fire."""

    status = Signal(object)


class _CastStatusForwarder:
    """Adapter to pychromecast's MediaController status-listener
    interface. The single required method is `new_media_status(status)`.
    We forward by emitting through the carrier signal so the GUI thread
    handles the actual translation to bus events."""

    def __init__(self, carrier: _CastStatusSignal):
        self._carrier = carrier

    def new_media_status(self, status):
        # Chromecast worker thread → carrier signal → Qt receiver thread.
        try:
            self._carrier.status.emit(status)
        except Exception:
            pass


class MpvController(QObject):
    """
    Single mpv instance, managed for the whole application.
    Headless by default; a video widget can attach to it later.
    """

    # Internal cross-thread signals (mpv callbacks fire on bg threads)
    _emit_position = Signal(int)
    _emit_duration = Signal(int)
    _emit_paused = Signal(bool)
    _emit_ended = Signal()
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

        # Crossfade scaffolding. Built lazily on first `_on_position` so
        # tests + cold-launch don't pay for a sibling handle that may
        # never fire. Gated by ``JT_CROSSFADE`` env + the runtime setting.
        # The ``QueueManager._build_now_playing`` payload exposes the
        # current queue item's raw dict via ``NowPlaying.raw`` — we
        # snapshot that here for the Crossfader's same-album check.
        self._crossfader: Optional[Crossfader] = None
        self._current_duration_ms: int = 0

        if not MPV_AVAILABLE:
            print(f"⚠️  mpv unavailable: {_MPV_ERROR}")
            print("   Install mpv from your package manager or https://mpv.io.")
            return

        self._init_mpv()
        self._connect_bus()

    # ── mpv setup ───────────────────────────────────────────────────────────

    def _make_mpv_handle(self, *, volume: Optional[int] = None) -> "mpv.MPV":
        """Build a fresh mpv handle with jellytoast's standard options.
        Extracted so the Crossfader sibling handle in
        ``modules/playback/crossfade.py`` can mint a second instance with
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
        if self.settings.gapless:
            kwargs["gapless_audio"] = "weak"
            kwargs["prefetch_playlist"] = "yes"
        # TODO platform: Windows WASAPI exclusive mode + raw ALSA without
        # dmix lock the second handle out. The research doc's plan is
        # ``audio-exclusive=no`` on both; defer to august after live
        # validation.
        return mpv.MPV(**kwargs)

    def _init_mpv(self):
        self._mpv = self._make_mpv_handle()

        # Property observers
        @self._mpv.property_observer("time-pos")
        def _on_time(_name, value):
            if value is not None:
                self._emit_position.emit(int(value * 1000))

        @self._mpv.property_observer("duration")
        def _on_dur(_name, value):
            if value is not None:
                self._emit_duration.emit(int(value * 1000))

        @self._mpv.property_observer("pause")
        def _on_pause(_name, value):
            self._emit_paused.emit(bool(value))

        @self._mpv.event_callback("end-file")
        def _on_end(event):
            try:
                reason = event.data.reason
                # mpv: 0=eof, 1=stop, 2=quit, 3=error, 4=redirect
                if reason in ("eof", 0):
                    self._emit_ended.emit()
            except Exception:
                pass

        # audio-bitrate updates per decode tick — far more often than
        # the UI needs. Rate-limit to one emit every 2s so the user
        # sees a stable readout instead of a jittering number. Reset
        # on play() so a new track gets its bitrate as soon as mpv
        # has it (not 2s into the new track).
        self._streaming_info_throttle_s = 2.0
        self._last_streaming_emit_t = 0.0

        @self._mpv.property_observer("audio-bitrate")
        def _on_audio_bitrate(_name, value):
            if not value or value < 1000:
                return
            now = time.monotonic()
            if now - self._last_streaming_emit_t < self._streaming_info_throttle_s:
                return
            self._last_streaming_emit_t = now
            try:
                codec = self._mpv.audio_codec_name or ""
            except Exception:
                codec = ""
            kbps = int(value / 1000)
            self._emit_streaming_info.emit(codec, kbps)

        # Live ICY title from internet-radio streams. mpv populates
        # ``metadata/by-key/icy-title`` for Icecast / Shoutcast feeds —
        # typically ``"Artist - Track"`` but station-specific (some
        # embed jingles, ad markers, or "Station ID" during fillers).
        # Property fires whenever the station bumps its now-playing
        # metadata; we filter empty + unchanged values so the bus only
        # sees real transitions. Tested manually against a live
        # Icecast feed — no headless harness covers this path because
        # an mpv instance with a real network stream is the only way
        # to make the property fire. Integration coverage is tracked
        # at docs/manual_test_plan.md §4 "Internet radio".
        self._last_radio_title = ""

        @self._mpv.property_observer("metadata/by-key/icy-title")
        def _on_icy_title(_name, value):
            title = (value or "").strip()
            if not title or title == self._last_radio_title:
                return
            self._last_radio_title = title
            # Diagnostic — surface what the station is broadcasting so
            # users + maintainers can confirm metadata is flowing.
            # Trimmed to a single line per change; safe to leave on.
            print(f"[radio] icy-title: {title!r}", flush=True)
            self._emit_radio_title.emit(title)

        # Wire cross-thread signals to bus (Qt-thread safe)
        self._emit_position.connect(self._on_position)
        self._emit_duration.connect(self._on_duration)
        self._emit_paused.connect(self._on_paused)
        self._emit_ended.connect(self._on_ended)
        self._emit_streaming_info.connect(self._on_streaming_info)
        self._emit_radio_title.connect(self._on_radio_title)

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
        # Re-apply the EQ filter at the head of every new track —
        # mpv's filter graph survives loadfile-replace in current
        # builds, but the `gapless_audio=weak` path occasionally
        # drops the graph when the sample rate changes across
        # tracks (44.1 → 48 transitions in particular). Re-asserting
        # the chain on playback_started is cheap when nothing has
        # changed (idempotent via ``_last_eq_state``) and keeps the
        # user's curve audible even when mpv re-plugs the output.
        self.bus.playback_started.connect(self._reapply_eq_on_start)
        self.bus.cast_started.connect(self._on_cast_started)
        self.bus.cast_stopped.connect(self._on_cast_stopped)
        self.bus.queue_prefetch_request.connect(self._on_prefetch_request)
        self.bus.playback_ended.connect(self._on_sleep_eot_check)
        self.bus.sleep_timer_requested.connect(self.start_sleep_timer)
        self.bus.sleep_timer_cancel_requested.connect(self.cancel_sleep_timer)

    # ── Cast routing ────────────────────────────────────────────────────────

    def set_cast_manager(self, cm):
        """Bind a CastManager. When `cm.active_cast` is set, transport
        commands route to the cast device instead of the local mpv
        instance — pause, seek, volume, and the next play() of a new
        track all go to the chromecast / airplay receiver."""
        self._cast_manager = cm

    def _cast_active(self):
        return self._cast_manager is not None and self._cast_manager.active_cast is not None

    def _ensure_cast_listener(self, cc):
        """Register the push-status listener on a cast object once. When
        the active device swaps (A → B), unregister from A first — the
        old device's pychromecast worker thread keeps emitting status
        forever otherwise, and our handler can't tell which device the
        push came from."""
        if cc is None or cc is self._cast_listener_attached_to:
            return
        old = self._cast_listener_attached_to
        if old is not None:
            try:
                # pychromecast doesn't expose remove_status_listener on
                # every version; the controller-internal status_listeners
                # list is the stable surface.
                listeners = getattr(old.media_controller, "status_listeners", None)
                if listeners is not None and self._cast_status_listener in listeners:
                    listeners.remove(self._cast_status_listener)
            except Exception as e:
                print(f"Cast listener deregister failed: {e}")
        try:
            cc.media_controller.register_status_listener(self._cast_status_listener)
            self._cast_listener_attached_to = cc
        except Exception as e:
            print(f"Cast listener register failed: {e}")

    # Default Chromecast volume on session start. Whatever the receiver
    # had stored from prior casts could be silent (you'd think nothing
    # connected) or full-blast (jarring). 30% is the uniform middle
    # that lets the user immediately confirm playback without panic-
    # reaching for the slider.
    _CAST_INITIAL_VOLUME = 30

    @Slot(str)
    def _on_cast_started(self, _name: str):
        if not self._cast_poll_timer.isActive():
            self._cast_poll_timer.start()
        if self._cast_manager is not None:
            self._cast_manager.chromecast_set_volume(self._CAST_INITIAL_VOLUME)
            # Push the new value into volume_state so the slider tracks
            # the device. set_volume's normal slider->bus path already
            # routes UI changes to the cast; this is the inverse — a
            # backend-side change needs to surface to the UI.
            self.bus.volume_state.emit(self._CAST_INITIAL_VOLUME)

    @Slot()
    def _on_cast_stopped(self):
        self._cast_poll_timer.stop()
        self._cast_last_player_state = None
        self._cast_last_duration_ms = -1
        self._cast_last_position_ms = -1
        self._cast_anchor_pos_ms = 0
        self._cast_anchor_wall = 0.0
        # Restore the slider (and mpv) to the user's pre-cast local
        # volume. settings.volume is preserved across the cast session
        # because set_volume skips the persist step while casting, so
        # this is the authoritative pre-cast value.
        local_vol = max(0, min(100, int(self.settings.volume)))
        if self._mpv is not None:
            try:
                self._mpv["volume"] = local_vol
            except Exception:
                pass
        self.bus.volume_state.emit(local_vol)

        # Hand the active track back to mpv at the cast's last-known
        # position, but start it paused — disconnecting from a cast is
        # a deliberate "pull it back to me" action, and the user gets
        # explicit control over the handoff by pressing play. Without
        # this, the play/pause button would lie about state because
        # mpv had no media loaded during the cast session.
        if self._mpv is None:
            return
        np = get_now_playing()
        if not np.item_id or not np.stream_url:
            self.bus.playback_stopped.emit()
            return
        try:
            start_sec = max(0.0, np.position / 1000.0)
            # Set the start property before loading and defer the
            # reset until *after* mpv has consumed it. The original
            # synchronous reset to "none" raced against mpv's async
            # loadfile processing — on slower backends mpv read the
            # property *after* our reset, and the offset got lost.
            # 750ms is generous: mpv consumes loadfile properties in
            # the same iteration of its event loop, so a deferred
            # reset always lands after.
            self._mpv["start"] = str(start_sec) if start_sec > 0.5 else "none"
            self._mpv["vid"] = "no" if np.is_audio else "auto"
            self._mpv["force-window"] = "no" if np.is_audio else "auto"
            self._mpv.play(np.stream_url)
            self._mpv["pause"] = True
            if start_sec > 0.5:
                QTimer.singleShot(750, lambda: self._reset_mpv_start())
            self._last_reported_position_ms = -1
            self._begin_play_session(np)
            self._report_session_start(np)
        except Exception as e:
            print(f"Cast → local handoff failed: {e}")
            self.bus.playback_stopped.emit()

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

    def _on_cast_status_push(self, status):
        """Push from the chromecast worker thread → marshal'd here on the
        GUI thread by _CastStatusSignal. Same translation logic as the
        polling path; both feed the existing mpv slots."""
        if not self._cast_active():
            return
        self._apply_cast_status(status)

    def _poll_cast_status(self):
        """Tick handler for the 500ms cast-poll timer. No-ops unless a
        chromecast session is active. Two jobs:
          1. Re-read mc.status in case a state change happened (the
             listener is supposed to push these but we keep the poll
             as a safety net).
          2. Emit an interpolated position between status pushes so
             the progress bar advances smoothly during playback —
             chromecast only pushes MEDIA_STATUS on state changes,
             not every second."""
        if not self._cast_active():
            return
        dev = self._cast_manager.active_cast
        if dev.device_type != "chromecast":
            # AirPlay (both v1 mDNS path and pyatv) has no programmatic
            # status channel we tap for periodic position updates here —
            # progress bar stays inert during AirPlay. (pyatv DOES expose
            # play_state, but the bar is wired off the Chromecast status
            # callback for now; revisit when the AirPlay 2 UI tightens.)
            return
        cc = dev.cast_object
        if cc is None:
            return
        # Make sure the push listener is wired for this device — covers
        # the case where active_cast was set without going through
        # MpvController.play (e.g., pre-connect via the cast dialog).
        self._ensure_cast_listener(cc)
        try:
            status = cc.media_controller.status
        except Exception:
            return
        self._apply_cast_status(status)

        # Interpolate position during playback. _apply_cast_status only
        # emits when chromecast reports a NEW current_time; between
        # those (~ every state change), the bar would freeze. Add the
        # wall-clock delta since the last anchor so it ticks in real
        # time. Cap at duration to avoid drifting past the end.
        if self._cast_last_player_state in ("PLAYING", "BUFFERING") and self._cast_anchor_wall > 0:
            elapsed_ms = int((self._monotonic() - self._cast_anchor_wall) * 1000)
            interp_ms = self._cast_anchor_pos_ms + elapsed_ms
            if self._cast_last_duration_ms > 0 and interp_ms > self._cast_last_duration_ms:
                interp_ms = self._cast_last_duration_ms
            # Push the interpolated value on the bus, but DON'T
            # update _cast_last_position_ms — that anchor only moves
            # when the chromecast itself reports a new value, so
            # we stay corrected on every push.
            self._on_position(interp_ms)

    def _apply_cast_status(self, status):
        # Player state first — anchoring depends on knowing whether
        # we're playing right now.
        ps = status.player_state
        if ps and ps != self._cast_last_player_state:
            prev = self._cast_last_player_state
            self._cast_last_player_state = ps
            if ps == "PAUSED":
                self._on_paused(True)
            elif ps in ("PLAYING", "BUFFERING"):
                self._on_paused(False)
            elif ps == "IDLE":
                # IDLE with idle_reason FINISHED = track ended cleanly.
                # Trigger _on_ended so the queue manager advances.
                # (IDLE on first connect doesn't have a previous PLAYING
                # state, so we gate on the transition.)
                if prev in ("PLAYING", "BUFFERING", "PAUSED"):
                    reason = getattr(status, "idle_reason", "") or ""
                    if reason.upper() == "FINISHED":
                        self._on_ended()

        # Re-anchor position from the chromecast's reported value
        # whenever it sends a fresh number. The poll path also runs
        # this; the anchored value is only updated when the chromecast
        # value differs from our last (so steady-state polling between
        # pushes doesn't keep re-anchoring to the same stale snapshot).
        ct = status.current_time
        if ct is not None:
            pos_ms = int(ct * 1000)
            if pos_ms != self._cast_last_position_ms:
                self._cast_last_position_ms = pos_ms
                self._cast_anchor_pos_ms = pos_ms
                self._cast_anchor_wall = self._monotonic()
                self._on_position(pos_ms)

        # Duration — emit on first non-zero value or when it changes
        # (track switch on the receiver triggers a new duration).
        dur = status.duration
        if dur is not None:
            dur_ms = int(dur * 1000)
            if dur_ms > 0 and dur_ms != self._cast_last_duration_ms:
                self._cast_last_duration_ms = dur_ms
                self._on_duration(dur_ms)

    # ── Playback session bookkeeping ────────────────────────────────────────

    def _resolve_play_method(self) -> str:
        """Derive the Jellyfin PlayMethod from the user's audio quality
        preference. "original" means we hand mpv `/Audio/{id}/stream`
        with `static=true` — the server ships bytes verbatim, mpv
        decodes locally → DirectStream. Any other value forces
        `/stream.mp3` with a MaxStreamingBitrate, which is a server-
        side transcode → Transcode. PlayMethod misreporting skews the
        admin transcoding stats so getting this right matters."""
        quality = (get_settings().audio_quality or "").strip().lower()
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
            if dev.device_type == "chromecast":
                # Pick the highest-quality URL + MIME the receiver can
                # direct-play. Chromecast handles MP3/FLAC/WAV/OGG/AAC
                # natively — for those we send the original-quality
                # /Audio/{id}/stream?static=true URL with the matching
                # content-type so FLAC stays FLAC, no transcoding.
                # Anything else (ALAC, DSD, etc.) falls back to a
                # 320kbps MP3 transcode the receiver definitely groks.
                from modules.cast_manager import CastManager

                container = (np.raw.get("Container") if np.raw else "") or ""
                url = np.stream_url
                mime = None
                if np.is_audio:
                    mime = CastManager.chromecast_audio_mime_for(container)
                    if mime is None:
                        # Build a transcoded MP3 URL via the provider so
                        # this stays correct on Subsonic / Navidrome
                        # (their /rest/stream endpoint is shaped
                        # differently from Jellyfin's /Audio/{id}/stream).
                        # Bypasses the user's audio_quality setting,
                        # which controls mpv local playback only.
                        url = self.api.get_audio_transcode_url(
                            np.item_id,
                            max_bitrate_kbps=320,
                            codec="mp3",
                        )
                        mime = "audio/mpeg"
                # Cast off the GUI thread — cast_to_chromecast blocks on
                # cc.wait() + block_until_active + the play-state poll.
                # Running it inline froze the UI for the length of every
                # track change while casting (the "locks up while
                # connecting" bug). The post-success bookkeeping moves
                # into the callback, which fires back on the GUI thread.
                self._cast_attempt += 1
                token = self._cast_attempt

                def _on_cast_done(ok: bool, _np=np, _t=token) -> None:
                    # Drop stale callbacks: if the user pressed Next
                    # again before this completed, a newer attempt is
                    # authoritative.
                    if _t != self._cast_attempt:
                        return
                    if ok:
                        self.bus.playback_started.emit(_np)
                        self._begin_play_session(_np)
                        self._report_session_start(_np)

                cm.cast_to_chromecast_async(
                    dev,
                    url,
                    np.title,
                    np.thumb_url,
                    is_audio=np.is_audio,
                    content_type=mime,
                    on_done=_on_cast_done,
                )
                return
            # AirPlay path stays synchronous for now.
            ok = cm.cast_to_airplay(dev, np.stream_url, np.title)
            if ok:
                self.bus.playback_started.emit(np)
                self._begin_play_session(np)
                self._report_session_start(np)
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
            self.bus.playback_started.emit(np)
            self._begin_play_session(np)
            self._report_session_start(np)
            self._last_reported_position_ms = -1
        except Exception as e:
            print(f"Play error: {e}")

    @Slot()
    def toggle_pause(self):
        if self._cast_active():
            self._cast_manager.chromecast_pause()
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
            print(f"toggle_pause failed: {e}")

    @Slot()
    def stop(self):
        if self._cast_active():
            # User pressed stop while casting — leave the session up
            # (handled by the dialog's Disconnect button), just halt
            # the current media on the receiver.
            self._cast_manager.chromecast_stop()
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
        when the env flag isn't set, the user hasn't enabled crossfade,
        a cast is active (the research doc §2 calls this out:
        crossfade is local-playback only), or mpv isn't available."""
        if not crossfade_env_enabled():
            return None
        if not self.settings.crossfade_enabled:
            return None
        if self._cast_active():
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

    def _abort_crossfade(self) -> None:
        """Called from explicit-action paths (play / stop / seek) so the
        Crossfader doesn't fight a transition the user just commanded."""
        if self._crossfader is not None:
            self._crossfader.abort()

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
            print(f"Prefetch append failed: {e}")

    @Slot(int)
    def seek(self, ms: int):
        if self._cast_active():
            self._cast_manager.chromecast_seek(ms / 1000.0)
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
        # Cast: best-effort relative seek using current position from
        # the receiver's media controller status.
        if self._cast_active():
            cm = self._cast_manager
            dev = cm.active_cast
            cc = dev.cast_object if dev.device_type == "chromecast" else None
            if cc is not None:
                try:
                    pos = cc.media_controller.status.current_time or 0
                    cm.chromecast_seek(max(0.0, pos + ms / 1000.0))
                except Exception:
                    pass
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
        if self._cast_active():
            # Don't persist cast volume into settings.volume — that
            # field is the user's local-playback preference and getting
            # polluted by cast adjustments would silently shift their
            # baseline every time they disconnect. The cast device
            # remembers its own state via the receiver session.
            self._cast_manager.chromecast_set_volume(vol)
            self.bus.volume_state.emit(vol)
            return
        if self._mpv is None:
            return
        try:
            self._mpv["volume"] = vol
            self.settings.volume = vol
            self.bus.volume_state.emit(vol)
        except Exception:
            pass

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
        from modules.eq_presets import BAND_COUNT, format_anequalizer_string

        enabled = bool(enabled)
        # Normalise the bands list to a tuple of floats so the
        # idempotence comparison doesn't fight list-vs-tuple or
        # mixed numeric types. A bad entry collapses to 0.0 here so
        # we always have a comparable shape, even on the disabled
        # path where the formatter won't run.
        try:
            normalised = tuple(float(b) for b in (bands or []))
        except (TypeError, ValueError):
            print("[jellytoast] apply_eq: non-numeric bands, skipping", flush=True)
            return
        if enabled and len(normalised) != BAND_COUNT:
            print(
                f"[jellytoast] apply_eq: expected {BAND_COUNT} bands, "
                f"got {len(normalised)} — skipping",
                flush=True,
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

        new_state = (enabled, normalised, preamp)
        if new_state == self._last_eq_state:
            return

        if self._mpv is None:
            self._last_eq_state = new_state
            return

        try:
            if not enabled:
                # Empty string clears mpv's audio-filter chain.
                # Setting to ``"no"`` would also work; the empty
                # form matches mpv's own "no filters" representation.
                self._mpv["af"] = ""
            else:
                # Chain: volume=<preamp>dB → anequalizer=<bands>.
                # Pre-amp first per docs/research/eq_dsp.md §3 so a
                # negative pre-amp gives headroom for the band boosts.
                # Drop the pre-amp filter entirely at 0 dB — keeps
                # the bypass path cheaper and the filter string short.
                chain = format_anequalizer_string(list(normalised))
                if abs(preamp) > 1e-9:
                    if float(preamp).is_integer():
                        p_str = str(int(preamp))
                    else:
                        p_str = f"{preamp:g}"
                    chain = f"volume={p_str}dB," + chain
                self._mpv["af"] = chain
        except Exception as e:
            print(f"[jellytoast] apply_eq failed: {e}", flush=True)
            return
        self._last_eq_state = new_state

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
        if self._mpv is None:
            return
        try:
            new_state = not self._mpv["mute"]
            self._mpv["mute"] = new_state
            self.bus.mute_state.emit(new_state)
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
        """Linearly ramp mpv volume to zero over `duration_ms`, then
        pause and restore the original volume so the next play session
        isn't silent."""
        if self._mpv is None:
            self.pause()
            return
        try:
            current = float(self._mpv["volume"] or 0)
        except Exception:
            self.pause()
            return
        duration_ms = max(self._SLEEP_FADE_TICK_MS, int(duration_ms))
        steps = max(1, duration_ms // self._SLEEP_FADE_TICK_MS)
        self._sleep_fade_original_volume = int(round(current))
        self._sleep_fade_current_volume = current
        self._sleep_fade_steps_remaining = steps
        self._sleep_fade_step_decrement = current / steps if steps else current
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
            self._finish_sleep_fade_and_pause()
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

    @Slot()
    def _on_sleep_eot_check(self):
        if not self._sleep_pending_eot:
            return
        self._sleep_pending_eot = False
        self.pause()

    def pause(self) -> None:
        """Idempotent pause — used by the sleep-timer fire paths. Routes
        through `toggle_pause` when mpv is actually playing so the cast
        branch + cold-launch promotion stay one code path."""
        if self._cast_active():
            try:
                self._cast_manager.chromecast_pause()
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
        if cf is not None:
            duration_ms = int(np.duration or self._current_duration_ms or 0)
            cf.on_position(ms, duration_ms)

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
        self.bus.streaming_info_updated.emit(codec, kbps)

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
