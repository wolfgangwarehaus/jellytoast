"""Cast transport + status plumbing for ``MpvController``.

Extracted from ``player_backend.py`` (the last god-file decomposition):
the local-vs-cast routing seam and the cross-thread chromecast-status
feed. ``MpvController`` mixes in ``_CastTransportMixin`` so these methods
resolve on the same instance — all ``self._cast_*`` state, the
``_cast_status_signal`` carrier, the poll timer, and the bus wiring stay
in ``MpvController.__init__`` / ``_connect_bus``; the mixin carries only
behaviour. Per-protocol transport lives in ``modules.cast_manager``; this
is the layer that marshals a cast device's status back onto the GUI
thread and drives the now-playing bar from it.

Kept as a mixin (not a composed collaborator) deliberately: a verbatim,
behaviour-preserving move that keeps the race-sensitive ``active_cast``
reads on the host instance rather than splitting cast state across a
callback boundary — the same call made for ``library_grid →
LibraryPaginator``.
"""

import logging

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from modules.cast_manager import CastType
from modules.player_state import get_now_playing

logger = logging.getLogger(__name__)


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


class _CastTransportMixin:
    """Cast routing + status methods for ``MpvController`` (see module
    docstring). No ``__init__`` and no Signals — the ``_cast_*`` state,
    the ``_cast_status_signal`` carrier, and the poll timer all live on
    the host; cross-calls (``_on_position`` / ``_on_duration`` /
    ``_on_paused`` / ``_on_ended`` / ``_reset_mpv_start`` …) resolve on
    the combined instance."""

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
                logger.warning("Cast listener deregister failed: %s", e)
        try:
            cc.media_controller.register_status_listener(self._cast_status_listener)
            self._cast_listener_attached_to = cc
        except Exception as e:
            logger.warning("Cast listener register failed: %s", e)

    # Default volume on cast session start — for every protocol, not
    # just Chromecast. Whatever the renderer had stored from prior casts
    # could be silent (you'd think nothing connected) or full-blast
    # (jarring) — DLNA TVs in particular replay the last thing watched at
    # its volume. 30% is the uniform middle that lets the user
    # immediately confirm playback without panic-reaching for the slider.
    _CAST_INITIAL_VOLUME = 30

    @Slot(str)
    def _on_cast_started(self, _name: str):
        if not self._cast_poll_timer.isActive():
            self._cast_poll_timer.start()
        if self._cast_manager is not None:
            # Route by device_type (DLNA/Sonos previously got nothing —
            # chromecast_set_volume early-returns off-Chromecast). Sonos
            # may clamp up to its volume floor, so emit the value that
            # was actually applied.
            applied = self._cast_manager.cast_set_initial_volume(self._CAST_INITIAL_VOLUME)
            # Push the new value into volume_state so the slider tracks
            # the device. set_volume's normal slider->bus path already
            # routes UI changes to the cast; this is the inverse — a
            # backend-side change needs to surface to the UI.
            self.bus.volume_state.emit(applied)

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
        # If the user had MUTED before/during the cast, restoring the
        # audible local volume above must also clear the mute flag and tell
        # the UI — otherwise the icon/slider stay "muted" while mpv plays at
        # the restored level (and the next mute-toggle would mis-track).
        if self._muted_volume is not None:
            self._muted_volume = None
            self.bus.mute_state.emit(False)

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
            # Load the handed-back track ALREADY PAUSED. Pausing only AFTER
            # play() left a brief async window between loadfile and the pause
            # taking effect, during which mpv emitted a fraction of a second of
            # audio at the restored local volume — heard as the intermittent
            # "volume spike" right as the cast disconnects. Setting pause BEFORE
            # play() makes mpv load the file in the paused state (idiomatic
            # --pause-on-load); the post-play set is belt-and-suspenders against
            # a loadfile that clears pause.
            self._mpv["pause"] = True
            self._mpv.play(np.stream_url)
            self._mpv["pause"] = True
            if start_sec > 0.5:
                QTimer.singleShot(750, lambda: self._reset_mpv_start())
            self._last_reported_position_ms = -1
            self._begin_play_session(np)
            self._report_session_start(np)
        except Exception as e:
            logger.warning("Cast → local handoff failed: %s", e)
            self.bus.playback_stopped.emit()

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
        if dev.device_type == CastType.DLNA:
            # DLNA: the controller runs its own 1 s GetPositionInfo poll
            # (started by cast_to_dlna). Read the cached snapshot off this
            # tick and drive the bar from it — no chromecast-style push
            # channel, so the bar would otherwise sit at 0:00.
            self._apply_dlna_status()
            return
        if dev.device_type != CastType.CHROMECAST:
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

    def _apply_dlna_status(self):
        """Drive the now-playing bar from the DLNA controller's polled
        transport snapshot (``transport_state`` / ``position_sec`` /
        ``duration_sec``), reusing the same ``_on_*`` emit helpers as the
        chromecast path. Called off the 500 ms cast-poll tick; the
        controller's own 1 s poll keeps ``last_state()`` fresh."""
        from modules.cast import dlna as _dlna

        st = _dlna.get_dlna_controller().last_state()
        if not st:
            return
        # transport_state is an async-upnp-client TransportState enum or "".
        ts = st.get("transport_state")
        tname = str(getattr(ts, "value", ts) or "").upper()
        if tname:
            prev = self._cast_last_player_state
            if tname in ("PLAYING", "TRANSITIONING"):
                if prev != "PLAYING":
                    self._cast_last_player_state = "PLAYING"
                    self._on_paused(False)
            elif tname in ("PAUSED_PLAYBACK", "PAUSED_RECORDING"):
                if prev != "PAUSED":
                    self._cast_last_player_state = "PAUSED"
                    self._on_paused(True)
            elif tname in ("STOPPED", "NO_MEDIA_PRESENT"):
                # Natural end-of-track while playing → advance the queue.
                # A user Disconnect tears down active_cast + the poll
                # first, so this only fires on a genuine track end.
                if prev in ("PLAYING", "PAUSED"):
                    self._cast_last_player_state = "STOPPED"
                    self._on_ended()
                return
        dur = st.get("duration_sec")
        if dur:
            dur_ms = int(dur * 1000)
            if dur_ms > 0 and dur_ms != self._cast_last_duration_ms:
                self._cast_last_duration_ms = dur_ms
                self._on_duration(dur_ms)
        pos = st.get("position_sec")
        if pos is not None:
            pos_ms = int(pos * 1000)
            if pos_ms != self._cast_last_position_ms:
                self._cast_last_position_ms = pos_ms
                self._on_position(pos_ms)
