"""Windows System Media Transport Controls (SMTC) backend.

Surfaces jellytoast in the Windows media flyout (the volume-key OSD's
now-playing card), the lock screen, and — crucially — makes hardware
media keys (Play/Pause/Next/Prev on keyboards and headsets) control the
app. This is the Windows equivalent of the Linux MPRIS backend.

Audio is owned by libmpv, not a WinRT ``MediaPlayer``, so we cannot use
``GetForCurrentView`` (needs a CoreWindow — absent in a desktop app).
The supported route is the interop ``GetForWindow(HWND)``, exposed by
PyWinRT as ``winrt.windows.media.interop.get_for_window`` — we hand it
the main window's HWND.

Design notes:
- All ``winrt`` imports are lazy (inside methods) so this module imports
  cleanly on every platform; ``start()`` no-ops gracefully if the winrt
  projection is missing or the HWND is unavailable.
- The SMTC ``ButtonPressed`` callback fires on a WinRT thread. We re-emit
  it as the Qt signal ``_button_pressed`` (signal emission is thread-
  safe) with a queued connection, so the actual handling — emitting
  PlayerBus commands — runs on the GUI thread (matches jellytoast's
  QObject thread-affinity rule).
- Default libmpv does NOT register SMTC (its ``media-controls`` option
  only affects the standalone mpv.exe), so we're the sole registrant —
  no mpv-side change is needed.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)

# SystemMediaTransportControlsButton enum values (stable WinRT ABI).
_BTN_PLAY = 0
_BTN_PAUSE = 1
_BTN_STOP = 2
_BTN_NEXT = 6
_BTN_PREVIOUS = 7


class WindowsMediaControlsService(QObject):
    """SMTC integration. Same shape as the MPRIS service (``start`` /
    ``stop``) so the facade swaps it in transparently on Windows."""

    # A media-key/flyout button press, marshalled off the WinRT thread.
    _button_pressed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._smtc = None
        self._updater = None
        self._bus = None
        self._token = None
        self._duration_ms = 0
        self._last_timeline_ms = -10_000
        self._button_pressed.connect(
            self._on_button, Qt.ConnectionType.QueuedConnection
        )

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, window=None):
        try:
            self._init_smtc(window)
        except Exception as e:
            # No winrt projection, no HWND, or an API shape mismatch —
            # degrade to no media-key integration rather than crash boot.
            logger.info("SMTC unavailable, media keys disabled: %s", e)
            self._smtc = None

    def _init_smtc(self, window):
        from winrt.windows.media import MediaPlaybackType
        from winrt.windows.media.interop import get_for_window

        hwnd = self._resolve_hwnd(window)
        if not hwnd:
            raise RuntimeError("no top-level HWND for SMTC")

        smtc = get_for_window(hwnd)
        smtc.is_enabled = True
        smtc.is_play_enabled = True
        smtc.is_pause_enabled = True
        smtc.is_stop_enabled = True
        smtc.is_next_enabled = True
        smtc.is_previous_enabled = True

        updater = smtc.display_updater
        updater.type = MediaPlaybackType.MUSIC

        self._token = smtc.add_button_pressed(self._raw_button_pressed)
        self._smtc = smtc
        self._updater = updater
        self._connect_bus()
        logger.info("SMTC registered (hwnd=%s)", hwnd)

    @staticmethod
    def _resolve_hwnd(window) -> int:
        if window is not None:
            try:
                return int(window.winId())
            except Exception:
                pass
        # Fallback: the first top-level widget with a native handle.
        try:
            from PySide6.QtWidgets import QApplication

            for w in QApplication.topLevelWidgets():
                if w.isWindow():
                    wid = int(w.winId())
                    if wid:
                        return wid
        except Exception:
            pass
        return 0

    def stop(self):
        smtc = self._smtc
        self._smtc = None
        self._updater = None
        if smtc is None:
            return
        try:
            if self._token is not None:
                smtc.remove_button_pressed(self._token)
            from winrt.windows.media import MediaPlaybackStatus

            smtc.playback_status = MediaPlaybackStatus.CLOSED
            smtc.is_enabled = False
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC teardown: %s", e)

    # ── PlayerBus → SMTC ───────────────────────────────────────────────

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
        if self._updater is None:
            return
        try:
            mp = self._updater.music_properties
            mp.title = getattr(np, "title", "") or ""
            mp.artist = getattr(np, "subtitle", "") or ""
            mp.album_title = getattr(np, "album", "") or ""
            self._set_thumbnail(getattr(np, "thumb_url", "") or "")
            self._updater.update()
            self._duration_ms = int(getattr(np, "duration", 0) or 0)
            self._last_timeline_ms = -10_000
            self._set_status("PLAYING")
            self._update_timeline(int(getattr(np, "position", 0) or 0))
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC metadata push failed: %s", e)

    def _set_thumbnail(self, url: str):
        if not url:
            return
        try:
            from winrt.windows.foundation import Uri
            from winrt.windows.storage.streams import RandomAccessStreamReference

            self._updater.thumbnail = RandomAccessStreamReference.create_from_uri(
                Uri(url)
            )
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC thumbnail failed: %s", e)

    @Slot()
    def _on_paused(self):
        self._set_status("PAUSED")

    @Slot()
    def _on_resumed(self):
        self._set_status("PLAYING")

    @Slot()
    def _on_stopped(self):
        self._set_status("STOPPED")

    def _set_status(self, name: str):
        if self._smtc is None:
            return
        try:
            from winrt.windows.media import MediaPlaybackStatus

            mapping = {
                "PLAYING": MediaPlaybackStatus.PLAYING,
                "PAUSED": MediaPlaybackStatus.PAUSED,
                "STOPPED": MediaPlaybackStatus.STOPPED,
            }
            self._smtc.playback_status = mapping.get(
                name, MediaPlaybackStatus.STOPPED
            )
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC status failed: %s", e)

    @Slot(int)
    def _on_duration(self, ms: int):
        self._duration_ms = int(ms or 0)

    @Slot(int)
    def _on_position(self, ms: int):
        if self._duration_ms <= 0:
            return
        # SMTC only needs an occasional timeline sync — throttle to ~5s,
        # but always push a backward jump (a seek) immediately.
        if ms < self._last_timeline_ms or ms - self._last_timeline_ms >= 5_000:
            self._update_timeline(ms)

    def _update_timeline(self, pos_ms: int):
        if self._smtc is None or self._duration_ms <= 0:
            return
        try:
            from datetime import timedelta

            from winrt.windows.media import (
                SystemMediaTransportControlsTimelineProperties,
            )

            tp = SystemMediaTransportControlsTimelineProperties()
            tp.start_time = timedelta(0)
            tp.min_seek_time = timedelta(0)
            tp.position = timedelta(milliseconds=max(0, pos_ms))
            tp.max_seek_time = timedelta(milliseconds=self._duration_ms)
            tp.end_time = timedelta(milliseconds=self._duration_ms)
            self._smtc.update_timeline_properties(tp)
            self._last_timeline_ms = pos_ms
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC timeline failed: %s", e)

    # ── SMTC → PlayerBus ───────────────────────────────────────────────

    def _raw_button_pressed(self, sender, args):
        """WinRT thread. Decode the button and re-emit on the GUI thread."""
        try:
            btn = args.button
            val = getattr(btn, "value", None)
            val = int(val) if val is not None else int(btn)
            self._button_pressed.emit(val)
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SMTC button decode failed: %s", e)

    @Slot(int)
    def _on_button(self, val: int):
        bus = self._bus
        if bus is None:
            return
        # Play and Pause both map to the toggle — SMTC sends Play only
        # when our status is Paused and Pause only when Playing, and we
        # keep that status in sync, so the toggle resolves correctly.
        if val in (_BTN_PLAY, _BTN_PAUSE):
            bus.pause_toggled.emit()
        elif val == _BTN_STOP:
            bus.stop_requested.emit()
        elif val == _BTN_NEXT:
            bus.next_track.emit()
        elif val == _BTN_PREVIOUS:
            bus.prev_track.emit()
