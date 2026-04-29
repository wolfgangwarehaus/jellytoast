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

import threading
from typing import Optional, Callable
from PyQt6.QtCore import (Qt, QObject, QTimer, pyqtSlot, pyqtSignal,
                           QMetaObject, Q_ARG)
from PyQt6.QtWidgets import QWidget

try:
    import mpv
    MPV_AVAILABLE = True
except (ImportError, OSError) as e:
    MPV_AVAILABLE = False
    _MPV_ERROR = str(e)

from modules.player_state import (PlayerBus, NowPlaying,
                                    get_now_playing, set_now_playing)
from modules.settings import get_settings
from modules.jellyfin_api import get_api


class MpvController(QObject):
    """
    Single mpv instance, managed for the whole application.
    Headless by default; a video widget can attach to it later.
    """

    # Internal cross-thread signals (mpv callbacks fire on bg threads)
    _emit_position = pyqtSignal(int)
    _emit_duration = pyqtSignal(int)
    _emit_paused = pyqtSignal(bool)
    _emit_ended = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_api()
        self._wid: Optional[int] = None
        self._mpv: Optional["mpv.MPV"] = None
        self._last_progress_report = 0

        if not MPV_AVAILABLE:
            print(f"⚠️  mpv unavailable: {_MPV_ERROR}")
            print("   Install with: sudo pacman -S mpv")
            return

        self._init_mpv()
        self._connect_bus()

        # Server-side progress reporting every 10s
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(10_000)
        self._progress_timer.timeout.connect(self._report_progress)

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
            audio_client_name="JellyPlayer",
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

        # Wire cross-thread signals to bus (Qt-thread safe)
        self._emit_position.connect(self._on_position)
        self._emit_duration.connect(self._on_duration)
        self._emit_paused.connect(self._on_paused)
        self._emit_ended.connect(self._on_ended)

    def _connect_bus(self):
        self.bus.play_requested.connect(self.play)
        self.bus.pause_toggled.connect(self.toggle_pause)
        self.bus.stop_requested.connect(self.stop)
        self.bus.seek_requested.connect(self.seek)
        self.bus.seek_relative.connect(self.seek_relative)
        self.bus.volume_changed.connect(self.set_volume)
        self.bus.mute_toggled.connect(self.toggle_mute)

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

    # ── Playback control ────────────────────────────────────────────────────

    @pyqtSlot(object)
    def play(self, np: NowPlaying):
        if self._mpv is None or not np.stream_url:
            return
        try:
            # Different presentation for audio vs video
            if np.is_audio:
                self._mpv["force-window"] = "no"
                self._mpv["vid"] = "no"
            else:
                self._mpv["vid"] = "auto"
            self._mpv.play(np.stream_url)
            self._mpv["pause"] = False
            self.bus.playback_started.emit(np)
            try:
                self.api.report_playback_start(np.item_id, np.position_ticks)
            except Exception:
                pass
            self._progress_timer.start()
        except Exception as e:
            print(f"Play error: {e}")

    @pyqtSlot()
    def toggle_pause(self):
        if self._mpv is None:
            return
        try:
            self._mpv["pause"] = not self._mpv["pause"]
        except Exception:
            pass

    @pyqtSlot()
    def stop(self):
        if self._mpv is None:
            return
        np = get_now_playing()
        try:
            self._mpv.stop()
        except Exception:
            pass
        self._progress_timer.stop()
        if np.item_id:
            try:
                self.api.report_playback_stopped(np.item_id, np.position_ticks)
            except Exception:
                pass
        self.bus.playback_stopped.emit()

    @pyqtSlot(int)
    def seek(self, ms: int):
        if self._mpv is None:
            return
        try:
            self._mpv.seek(ms / 1000.0, reference="absolute")
        except Exception:
            pass

    @pyqtSlot(int)
    def seek_relative(self, ms: int):
        if self._mpv is None:
            return
        try:
            self._mpv.seek(ms / 1000.0, reference="relative")
        except Exception:
            pass

    @pyqtSlot(int)
    def set_volume(self, vol: int):
        if self._mpv is None:
            return
        vol = max(0, min(100, vol))
        try:
            self._mpv["volume"] = vol
            self.settings.volume = vol
            self.bus.volume_state.emit(vol)
        except Exception:
            pass

    @pyqtSlot()
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

    @pyqtSlot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        np.position = ms
        self.bus.position_updated.emit(ms)

    @pyqtSlot(int)
    def _on_duration(self, ms: int):
        np = get_now_playing()
        if ms > 0 and np.duration == 0:
            np.duration = ms
        self.bus.duration_set.emit(ms)

    @pyqtSlot(bool)
    def _on_paused(self, paused: bool):
        np = get_now_playing()
        np.is_paused = paused
        if paused:
            self.bus.playback_paused.emit()
        else:
            self.bus.playback_resumed.emit()

    @pyqtSlot()
    def _on_ended(self):
        np = get_now_playing()
        if np.item_id:
            try:
                self.api.mark_played(np.item_id)
            except Exception:
                pass
        self._progress_timer.stop()
        self.bus.playback_ended.emit()

    # ── Server progress reporting ───────────────────────────────────────────

    def _report_progress(self):
        np = get_now_playing()
        if not np.item_id:
            return
        try:
            self.api.report_playback_progress(
                np.item_id, np.position_ticks, np.is_paused
            )
        except Exception:
            pass

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self):
        if self._mpv is not None:
            try:
                np = get_now_playing()
                if np.item_id:
                    self.api.report_playback_stopped(np.item_id, np.position_ticks)
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
