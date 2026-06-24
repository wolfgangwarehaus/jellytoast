"""macOS NowPlaying / Control Center media-controls backend.

Surfaces jellytoast in Control Center's Now Playing module, on the lock
screen, and — crucially — makes hardware media keys (the F7/F8/F9 row and
bluetooth headset buttons) control the app. This is the macOS equivalent of
the Linux MPRIS (`_mpris`) and Windows SMTC (`_windows`) backends.

Uses the MediaPlayer framework via pyobjc:
- ``MPNowPlayingInfoCenter`` — pushes title / artist / album / duration /
  elapsed + playback state so the OS renders the Now Playing card.
- ``MPRemoteCommandCenter`` — registers Play / Pause / Toggle / Next / Prev /
  Stop / Seek handlers; this is how the OS routes media keys back to us.

Audio is owned by libmpv, not an ``AVAudioPlayer``, so we are the sole
NowPlaying registrant — no mpv-side change is needed (default libmpv does
NOT register NowPlaying; its ``media-controls`` option only affects the
standalone ``mpv`` binary).

Design notes:
- All pyobjc imports are lazy (inside ``start``) so this module imports
  cleanly on every platform; ``start()`` degrades to a silent no-op if the
  framework or any symbol is missing.
- ``MPRemoteCommand`` handlers are delivered on the main (GUI) thread, but we
  still marshal them through a queued Qt signal before touching the
  ``PlayerBus`` — matching jellytoast's QObject thread-affinity rule and
  staying correct even if a future macOS delivers a handler off-thread.
- Durations/positions on the bus are milliseconds; MediaPlayer wants seconds.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)

# Our own command ids — what a remote command maps to once marshalled onto
# the GUI thread (kept tiny + backend-private; mirrors the Windows _BTN_*).
_CMD_TOGGLE = 0
_CMD_PLAY = 1
_CMD_PAUSE = 2
_CMD_STOP = 3
_CMD_NEXT = 4
_CMD_PREV = 5

# MPRemoteCommandHandlerStatusSuccess — the handler return value telling the
# OS the command was accepted (stable ABI; pyobjc also exposes it by name).
_HANDLER_SUCCESS = 0


class MacMediaControlsService(QObject):
    """macOS NowPlaying integration. Same shape as the MPRIS / SMTC services
    (``start`` / ``stop``) so the facade swaps it in transparently on macOS."""

    # A remote command / scrubber event, marshalled off the (possibly main)
    # callback context onto the GUI thread via a queued connection.
    _command = Signal(int)
    _seek = Signal(float)  # absolute position, seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self._MP = None
        self._info_center = None
        self._cmd_center = None
        self._bus = None
        self._info: dict = {}
        self._duration_s = 0.0
        self._last_pushed_s = -10.0
        # (command, target) pairs kept so stop() can detach them, and so the
        # handler blocks stay referenced (pyobjc must not GC a live block).
        self._targets: list = []
        self._command.connect(self._on_command, Qt.ConnectionType.QueuedConnection)
        self._seek.connect(self._on_seek, Qt.ConnectionType.QueuedConnection)

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, window=None):
        try:
            self._init_nowplaying()
        except Exception as e:
            # No pyobjc projection, missing symbol, or an API shape mismatch —
            # degrade to no media-key integration rather than crash boot.
            logger.info("macOS NowPlaying unavailable, media keys disabled: %s", e)
            self._info_center = None

    def _init_nowplaying(self):
        import MediaPlayer as MP

        self._MP = MP
        self._info_center = MP.MPNowPlayingInfoCenter.defaultCenter()
        cc = MP.MPRemoteCommandCenter.sharedCommandCenter()
        self._cmd_center = cc

        def reg(command, cmd_id):
            command.setEnabled_(True)

            def handler(event, _id=cmd_id):
                self._command.emit(_id)
                return _HANDLER_SUCCESS

            target = command.addTargetWithHandler_(handler)
            # Keep BOTH the python handler and the opaque target alive.
            self._targets.append((command, target, handler))

        reg(cc.togglePlayPauseCommand(), _CMD_TOGGLE)
        reg(cc.playCommand(), _CMD_PLAY)
        reg(cc.pauseCommand(), _CMD_PAUSE)
        reg(cc.stopCommand(), _CMD_STOP)
        reg(cc.nextTrackCommand(), _CMD_NEXT)
        reg(cc.previousTrackCommand(), _CMD_PREV)

        # Scrubber drag on the Now Playing card / Control Center.
        seek_cmd = cc.changePlaybackPositionCommand()
        seek_cmd.setEnabled_(True)

        def seek_handler(event):
            try:
                self._seek.emit(float(event.positionTime()))
            except Exception:
                pass
            return _HANDLER_SUCCESS

        target = seek_cmd.addTargetWithHandler_(seek_handler)
        self._targets.append((seek_cmd, target, seek_handler))

        self._connect_bus()
        logger.info("macOS NowPlaying registered (MPNowPlayingInfoCenter)")

    def stop(self):
        ic = self._info_center
        self._info_center = None
        MP = self._MP
        if ic is not None and MP is not None:
            try:
                ic.setPlaybackState_(MP.MPNowPlayingPlaybackStateStopped)
                ic.setNowPlayingInfo_(None)
            except Exception as e:  # pragma: no cover — macOS-only
                logger.debug("NowPlaying teardown: %s", e)
        for command, target, _handler in self._targets:
            try:
                command.removeTarget_(target)
            except Exception:  # pragma: no cover — macOS-only
                pass
        self._targets.clear()

    # ── PlayerBus → NowPlaying ─────────────────────────────────────────

    def _connect_bus(self):
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        self._bus = bus
        bus.playback_started.connect(self._on_started)
        bus.playback_paused.connect(self._on_paused)
        bus.playback_resumed.connect(self._on_resumed)
        bus.playback_stopped.connect(self._on_stopped)
        bus.playback_ended.connect(self._on_stopped)
        bus.position_updated.connect(self._on_position)
        bus.duration_set.connect(self._on_duration)

    @Slot(object)
    def _on_started(self, np):
        if self._info_center is None:
            return
        MP = self._MP
        try:
            self._duration_s = float(getattr(np, "duration", 0) or 0) / 1000.0
            info = {
                MP.MPMediaItemPropertyTitle: getattr(np, "title", "") or "",
                MP.MPMediaItemPropertyArtist: getattr(np, "subtitle", "") or "",
                MP.MPMediaItemPropertyAlbumTitle: getattr(np, "album", "") or "",
                MP.MPMediaItemPropertyPlaybackDuration: self._duration_s,
                MP.MPNowPlayingInfoPropertyElapsedPlaybackTime: float(
                    getattr(np, "position", 0) or 0
                )
                / 1000.0,
                MP.MPNowPlayingInfoPropertyPlaybackRate: 1.0,
            }
            self._info = info
            self._last_pushed_s = -10.0
            self._info_center.setNowPlayingInfo_(info)
            self._info_center.setPlaybackState_(MP.MPNowPlayingPlaybackStatePlaying)
            self._set_artwork(getattr(np, "thumb_url", "") or "")
        except Exception as e:  # pragma: no cover — macOS-only
            logger.debug("NowPlaying metadata push failed: %s", e)

    def _set_artwork(self, url: str):
        """Best-effort cover art. Loads the thumbnail into an
        ``MPMediaItemArtwork`` and merges it into the current info dict.
        Swallows everything — art is cosmetic and must never break boot or
        a track change."""
        if not url:
            return
        try:
            from Cocoa import NSImage
            from Foundation import NSURL

            MP = self._MP
            nsurl = (
                NSURL.URLWithString_(url)
                if "://" in url
                else NSURL.fileURLWithPath_(url)
            )
            img = NSImage.alloc().initWithContentsOfURL_(nsurl)
            if img is None:
                return
            size = img.size()

            def _provider(bounds, _img=img):
                return _img

            art = MP.MPMediaItemArtwork.alloc().initWithBoundsSize_requestHandler_(
                size, _provider
            )
            if self._info_center is None:
                return
            self._info = {**self._info, MP.MPMediaItemPropertyArtwork: art}
            self._info_center.setNowPlayingInfo_(self._info)
        except Exception as e:  # pragma: no cover — macOS-only
            logger.debug("NowPlaying artwork failed: %s", e)

    @Slot()
    def _on_paused(self):
        self._set_state("Paused")

    @Slot()
    def _on_resumed(self):
        self._set_state("Playing")

    @Slot()
    def _on_stopped(self):
        self._set_state("Stopped")

    def _set_state(self, name: str):
        if self._info_center is None:
            return
        MP = self._MP
        try:
            mapping = {
                "Playing": MP.MPNowPlayingPlaybackStatePlaying,
                "Paused": MP.MPNowPlayingPlaybackStatePaused,
                "Stopped": MP.MPNowPlayingPlaybackStateStopped,
            }
            self._info_center.setPlaybackState_(
                mapping.get(name, MP.MPNowPlayingPlaybackStateStopped)
            )
            # Keep the interpolated elapsed time honest: the OS extrapolates
            # from ElapsedPlaybackTime using PlaybackRate, so flip the rate
            # to 0 when not playing.
            if self._info:
                self._info[MP.MPNowPlayingInfoPropertyPlaybackRate] = (
                    1.0 if name == "Playing" else 0.0
                )
                self._info_center.setNowPlayingInfo_(self._info)
        except Exception as e:  # pragma: no cover — macOS-only
            logger.debug("NowPlaying state failed: %s", e)

    @Slot(int)
    def _on_duration(self, ms: int):
        self._duration_s = float(ms or 0) / 1000.0
        if self._info and self._info_center is not None:
            try:
                self._info[self._MP.MPMediaItemPropertyPlaybackDuration] = (
                    self._duration_s
                )
                self._info_center.setNowPlayingInfo_(self._info)
            except Exception as e:  # pragma: no cover — macOS-only
                logger.debug("NowPlaying duration failed: %s", e)

    @Slot(int)
    def _on_position(self, ms: int):
        # The OS interpolates elapsed time from the last push + rate, so we
        # only need to re-anchor occasionally — throttle to ~5s, but always
        # push a backward jump (a seek) immediately. Mirrors the SMTC backend.
        if self._info_center is None or not self._info:
            return
        pos_s = float(ms or 0) / 1000.0
        if pos_s >= self._last_pushed_s and pos_s - self._last_pushed_s < 5.0:
            return
        try:
            self._info[self._MP.MPNowPlayingInfoPropertyElapsedPlaybackTime] = pos_s
            self._info_center.setNowPlayingInfo_(self._info)
            self._last_pushed_s = pos_s
        except Exception as e:  # pragma: no cover — macOS-only
            logger.debug("NowPlaying position failed: %s", e)

    # ── NowPlaying → PlayerBus ─────────────────────────────────────────

    @Slot(int)
    def _on_command(self, cmd_id: int):
        bus = self._bus
        if bus is None:
            return
        # Play / Pause / Toggle all resolve to the single toggle — the OS
        # sends Play only when our state is Paused and Pause only when
        # Playing, and we keep that state in sync, so the toggle is correct.
        if cmd_id in (_CMD_TOGGLE, _CMD_PLAY, _CMD_PAUSE):
            bus.pause_toggled.emit()
        elif cmd_id == _CMD_STOP:
            bus.stop_requested.emit()
        elif cmd_id == _CMD_NEXT:
            bus.next_track.emit()
        elif cmd_id == _CMD_PREV:
            bus.prev_track.emit()

    @Slot(float)
    def _on_seek(self, seconds: float):
        bus = self._bus
        if bus is None:
            return
        bus.seek_requested.emit(int(max(0.0, seconds) * 1000))
