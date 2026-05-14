"""
mpv-based playback backend.

Why mpv:
- Bit-perfect audio (FLAC, ALAC, OPUS, DSD) — no transcoding
- Gapless playback for albums
- ReplayGain support
- Hardware-accelerated video decoding
- Lower CPU/RAM than browser-based playback
- Native to Linux/Arch

This module exposes:
- MpvController (headless audio + signal wiring)
- MpvVideoWidget (Qt widget with embedded mpv video output)
"""

import time
import uuid
from typing import Optional
from PySide6.QtCore import (Qt, QObject, QTimer, Slot, Signal)
from PySide6.QtWidgets import QWidget

try:
    import mpv
    MPV_AVAILABLE = True
except (ImportError, OSError) as e:
    MPV_AVAILABLE = False
    _MPV_ERROR = str(e)

from modules.player_state import (PlayerBus, NowPlaying,
                                    get_now_playing)
from modules.settings import get_settings
from modules.providers import get_provider


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_provider()
        self._wid: Optional[int] = None
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

        if not MPV_AVAILABLE:
            print(f"⚠️  mpv unavailable: {_MPV_ERROR}")
            print("   Install mpv from your package manager or https://mpv.io.")
            return

        self._init_mpv()
        self._connect_bus()

    # ── mpv setup ───────────────────────────────────────────────────────────

    def _init_mpv(self):
        kwargs = dict(
            ytdl=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            keep_open="no",
            idle="yes",
            force_window="no",
            audio_display="no",       # no album-art on its own video output
            hwdec="auto-safe",
            cache="yes",
            demuxer_max_bytes="100MiB",
            volume=self.settings.volume,
            replaygain=self.settings.replaygain,
            replaygain_clip="no",
            audio_client_name="JellyToast",
        )
        if self.settings.gapless:
            kwargs["gapless_audio"] = "weak"
            kwargs["prefetch_playlist"] = "yes"

        self._mpv = mpv.MPV(**kwargs)

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
            if (now - self._last_streaming_emit_t
                    < self._streaming_info_throttle_s):
                return
            self._last_streaming_emit_t = now
            try:
                codec = self._mpv.audio_codec_name or ""
            except Exception:
                codec = ""
            kbps = int(value / 1000)
            self._emit_streaming_info.emit(codec, kbps)

        # Wire cross-thread signals to bus (Qt-thread safe)
        self._emit_position.connect(self._on_position)
        self._emit_duration.connect(self._on_duration)
        self._emit_paused.connect(self._on_paused)
        self._emit_ended.connect(self._on_ended)
        self._emit_streaming_info.connect(self._on_streaming_info)

    def _connect_bus(self):
        self.bus.play_requested.connect(self.play)
        self.bus.pause_toggled.connect(self.toggle_pause)
        self.bus.stop_requested.connect(self.stop)
        self.bus.seek_requested.connect(self.seek)
        self.bus.seek_relative.connect(self.seek_relative)
        self.bus.volume_changed.connect(self.set_volume)
        self.bus.mute_toggled.connect(self.toggle_mute)
        self.bus.replaygain_changed.connect(self.set_replaygain)
        self.bus.cast_started.connect(self._on_cast_started)
        self.bus.cast_stopped.connect(self._on_cast_stopped)
        self.bus.queue_prefetch_request.connect(self._on_prefetch_request)

    # ── Video output attachment ─────────────────────────────────────────────

    def attach_video_widget(self, wid: int):
        """Bind mpv's video output to a Qt widget's native window id."""
        if self._mpv is None:
            return
        self._wid = wid
        try:
            self._mpv["wid"] = str(wid)
        except Exception as e:
            print(f"Failed to attach video widget: {e}")

    # ── Cast routing ────────────────────────────────────────────────────────

    def set_cast_manager(self, cm):
        """Bind a CastManager. When `cm.active_cast` is set, transport
        commands route to the cast device instead of the local mpv
        instance — pause, seek, volume, and the next play() of a new
        track all go to the chromecast / airplay receiver."""
        self._cast_manager = cm

    def _cast_active(self):
        return (self._cast_manager is not None
                and self._cast_manager.active_cast is not None)

    def _ensure_cast_listener(self, cc):
        """Register the push-status listener on a cast object once.
        Re-registering after a reconnect is harmless; pychromecast
        dedupes by listener identity."""
        if cc is None or cc is self._cast_listener_attached_to:
            return
        try:
            cc.media_controller.register_status_listener(
                self._cast_status_listener
            )
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
            # AirPlay v1 has no programmatic status channel — would
            # need DACP/RAOP2. Progress bar stays inert during AirPlay.
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
        if (self._cast_last_player_state in ("PLAYING", "BUFFERING")
                and self._cast_anchor_wall > 0):
            elapsed_ms = int(
                (self._monotonic() - self._cast_anchor_wall) * 1000
            )
            interp_ms = self._cast_anchor_pos_ms + elapsed_ms
            if (self._cast_last_duration_ms > 0
                    and interp_ms > self._cast_last_duration_ms):
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
            position_ticks = (
                np.duration_ticks if force_finished else np.position_ticks
            )
        else:
            position_ticks = 0
        try:
            self.api.report_playback_stopped(
                self._session_item_id, position_ticks,
                play_session_id=self._session_id,
                play_method=self._session_play_method,
            )
        except Exception:
            pass
        self._session_item_id = ""
        self._session_id = ""
        self._session_play_method = "DirectStream"

    def _report_session_start(self, np: NowPlaying):
        """Wrapper around api.report_playback_start that always pulls
        the session id + play method we minted in _begin_play_session."""
        try:
            self.api.report_playback_start(
                np.item_id, np.position_ticks,
                play_session_id=self._session_id,
                play_method=self._session_play_method,
            )
        except Exception:
            pass

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
                            np.item_id, max_bitrate_kbps=320, codec="mp3",
                        )
                        mime = "audio/mpeg"
                # Cast off the GUI thread — cast_to_chromecast blocks on
                # cc.wait() + block_until_active + the play-state poll.
                # Running it inline froze the UI for the length of every
                # track change while casting (the "locks up while
                # connecting" bug). The post-success bookkeeping moves
                # into the callback, which fires back on the GUI thread.
                def _on_cast_done(ok: bool, _np=np) -> None:
                    if ok:
                        self.bus.playback_started.emit(_np)
                        self._begin_play_session(_np)
                        self._report_session_start(_np)
                cm.cast_to_chromecast_async(
                    dev, url, np.title, np.thumb_url,
                    is_audio=np.is_audio, content_type=mime,
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
            self._mpv.pause = not self._mpv.pause
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
        if self._last_reported_position_ms < 0 or \
                abs(ms - self._last_reported_position_ms) >= self.PROGRESS_REPORT_DELTA_MS:
            self._last_reported_position_ms = ms
            self._report_progress()
            if np.item_id:
                self.settings.saved_position_ms = ms
                self.settings.saved_position_item_id = np.item_id

    @Slot(int)
    def _on_duration(self, ms: int):
        np = get_now_playing()
        if ms > 0 and np.duration == 0:
            np.duration = ms
        self.bus.duration_set.emit(ms)

    @Slot(bool)
    def _on_paused(self, paused: bool):
        np = get_now_playing()
        np.is_paused = paused
        # The pause property observer fires once on registration with
        # mpv's default pause=False — even when mpv has nothing loaded.
        # Without this gate, that initial fire emits playback_resumed
        # at boot and clobbers the resume icon (which should read as
        # "play / paused at saved position"). Gate on `path` so we
        # only forward state changes when a file is actually loaded.
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

    # ── Server progress reporting ───────────────────────────────────────────

    def _report_progress(self):
        np = get_now_playing()
        if not np.item_id:
            return
        try:
            self.api.report_playback_progress(
                np.item_id, np.position_ticks, np.is_paused,
                play_session_id=self._session_id,
                play_method=self._session_play_method,
            )
        except Exception:
            pass

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self):
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


# ── Embeddable video widget ─────────────────────────────────────────────────

class MpvVideoWidget(QWidget):
    """
    Qt widget that hosts mpv's video output via the X11/Wayland window id.
    Use Qt.WA_NativeWindow + WA_DontCreateNativeAncestors so mpv can render here.
    """

    def __init__(self, controller: MpvController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        # Defer attaching until winId() is valid
        QTimer.singleShot(0, self._attach)

    def _attach(self):
        wid = int(self.winId())
        if wid:
            self.controller.attach_video_widget(wid)

    def mouseDoubleClickEvent(self, event):
        # Toggle fullscreen on double-click
        win = self.window()
        if win.isFullScreen():
            win.showNormal()
        else:
            win.showFullScreen()
