"""
MPRIS2 D-Bus media player interface.

Implements org.mpris.MediaPlayer2 and org.mpris.MediaPlayer2.Player so:
- Keyboard media keys (Play/Pause/Next/Prev) work system-wide
- KDE Plasma media widget shows now-playing
- GNOME Shell media controls show now-playing
- waybar / playerctl / wlogout etc. integrate
- Other apps (e.g. browsers) yield audio focus appropriately

Runs dbus-next on a background asyncio loop. Communicates with the Qt main
thread via PlayerBus signals (which are thread-safe).
"""

import asyncio
import threading
from typing import Optional, List
from PySide6.QtCore import QObject, QTimer, Slot

try:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method, dbus_property, signal
    from dbus_next.constants import PropertyAccess
    from dbus_next import Variant, BusType
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

from modules.player_state import PlayerBus, NowPlaying, get_now_playing
from modules.settings import get_settings


SERVICE_NAME = "org.mpris.MediaPlayer2.JellyToast"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


if DBUS_AVAILABLE:

    class MprisRoot(ServiceInterface):
        """org.mpris.MediaPlayer2 — root interface."""

        def __init__(self, bus: PlayerBus):
            super().__init__("org.mpris.MediaPlayer2")
            self._bus = bus

        @method()
        def Raise(self):
            self._bus.open_main_window.emit()

        @method()
        def Quit(self):
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "JellyToast"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "jellytoast"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return ["http", "https"]

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return [
                "audio/mpeg", "audio/flac", "audio/x-flac", "audio/ogg",
                "audio/x-vorbis+ogg", "audio/aac", "audio/x-aac",
                "audio/mp4", "audio/wav",
                "video/mp4", "video/x-matroska", "video/webm",
                "application/x-mpegURL",
            ]


    class MprisPlayer(ServiceInterface):
        """org.mpris.MediaPlayer2.Player — playback interface."""

        def __init__(self, bus: PlayerBus):
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._bus = bus
            self._status = "Stopped"   # Playing | Paused | Stopped
            self._loop = "None"        # None | Track | Playlist
            self._shuffle = False
            self._volume = get_settings().volume / 100.0
            self._position = 0         # microseconds
            self._metadata: dict = {}
            self._can_go_next = False
            self._can_go_prev = False

        # ── Methods ─────────────────────────────────────────────────────────
        @method()
        def Next(self):
            self._bus.next_track.emit()

        @method()
        def Previous(self):
            self._bus.prev_track.emit()

        @method()
        def Pause(self):
            np = get_now_playing()
            if not np.is_paused:
                self._bus.pause_toggled.emit()

        @method()
        def PlayPause(self):
            self._bus.pause_toggled.emit()

        @method()
        def Stop(self):
            self._bus.stop_requested.emit()

        @method()
        def Play(self):
            np = get_now_playing()
            if np.is_paused:
                self._bus.pause_toggled.emit()

        @method()
        def Seek(self, offset: "x"):
            # offset in microseconds
            self._bus.seek_relative.emit(int(offset / 1000))

        @method()
        def SetPosition(self, track_id: "o", position: "x"):
            self._bus.seek_requested.emit(int(position / 1000))

        @method()
        def OpenUri(self, uri: "s"):
            pass  # Could be implemented to handle jellyfin:// URIs

        # ── Signals ─────────────────────────────────────────────────────────
        @signal()
        def Seeked(self, position: "x") -> "x":
            return position

        # ── Properties ──────────────────────────────────────────────────────
        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return self._status

        @dbus_property()
        def LoopStatus(self) -> "s":
            return self._loop

        @LoopStatus.setter
        def LoopStatus(self, val: "s"):
            self._loop = val
            mode = {"None": "off", "Track": "one", "Playlist": "all"}.get(val, "off")
            self._bus.repeat_changed.emit(mode)

        @dbus_property()
        def Rate(self) -> "d":
            return 1.0

        @Rate.setter
        def Rate(self, val: "d"):
            pass

        @dbus_property()
        def Shuffle(self) -> "b":
            return self._shuffle

        @Shuffle.setter
        def Shuffle(self, val: "b"):
            self._shuffle = val
            self._bus.shuffle_changed.emit(val)

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return self._metadata

        @dbus_property()
        def Volume(self) -> "d":
            return self._volume

        @Volume.setter
        def Volume(self, val: "d"):
            self._volume = max(0.0, min(1.0, val))
            self._bus.volume_changed.emit(int(self._volume * 100))

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return self._position

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return self._can_go_next

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return self._can_go_prev

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

        # ── State updaters (called from Qt thread, scheduled on dbus loop) ──
        def update_status(self, status: str):
            self._status = status
            self.emit_properties_changed({"PlaybackStatus": status})

        def update_metadata(self, np: NowPlaying):
            track_id = f"/org/jellytoast/track/{np.item_id}" if np.item_id else "/"
            md = {
                "mpris:trackid": Variant("o", track_id),
                "mpris:length": Variant("x", np.duration_ticks // 10),  # microseconds
                "xesam:title": Variant("s", np.title or ""),
                "xesam:album": Variant("s", np.album or ""),
                "xesam:artist": Variant("as", [np.subtitle] if np.subtitle else []),
                "xesam:albumArtist": Variant("as", [np.subtitle] if np.subtitle else []),
            }
            if np.thumb_url:
                md["mpris:artUrl"] = Variant("s", np.thumb_url)
            if np.year:
                md["xesam:contentCreated"] = Variant("s", f"{np.year}-01-01T00:00:00Z")
            self._metadata = md
            self.emit_properties_changed({"Metadata": md})

        def update_position(self, ms: int):
            self._position = ms * 1000  # microseconds

        def update_volume(self, vol: int):
            self._volume = vol / 100.0
            self.emit_properties_changed({"Volume": self._volume})

        def update_can_next_prev(self, has_next: bool, has_prev: bool):
            self._can_go_next = has_next
            self._can_go_prev = has_prev
            self.emit_properties_changed({
                "CanGoNext": has_next, "CanGoPrevious": has_prev,
            })

        def emit_seeked(self, ms: int):
            self.Seeked(ms * 1000)


class MprisService(QObject):
    """
    Qt-side controller for the MPRIS service.
    Runs dbus-next on a separate asyncio thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._root: Optional["MprisRoot"] = None
        self._player: Optional["MprisPlayer"] = None
        self._dbus = None
        self._ready = threading.Event()

    def start(self):
        if not DBUS_AVAILABLE:
            print("ℹ️  D-Bus not available; MPRIS disabled.")
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait briefly for setup
        if self._ready.wait(timeout=3.0):
            self._connect_signals()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._setup())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:
            print(f"MPRIS error: {e}")
            self._ready.set()

    async def _setup(self):
        self._dbus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._root = MprisRoot(self.bus)
        self._player = MprisPlayer(self.bus)
        self._dbus.export(OBJECT_PATH, self._root)
        self._dbus.export(OBJECT_PATH, self._player)
        await self._dbus.request_name(SERVICE_NAME)

    # ── Forward Qt signals to dbus-next via call_soon_threadsafe ────────────

    def _connect_signals(self):
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_paused.connect(lambda: self._update_status("Paused"))
        self.bus.playback_resumed.connect(lambda: self._update_status("Playing"))
        self.bus.playback_stopped.connect(lambda: self._update_status("Stopped"))
        self.bus.position_updated.connect(self._on_position)
        self.bus.volume_state.connect(self._on_volume)
        self.bus.queue_changed.connect(self._on_queue_changed)

    def _schedule(self, coro_or_call):
        if self._loop and self._player:
            self._loop.call_soon_threadsafe(coro_or_call)

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        if not self._player:
            return
        self._schedule(lambda: self._player.update_metadata(np))
        self._schedule(lambda: self._player.update_status("Playing"))

    def _update_status(self, status: str):
        if self._player:
            self._schedule(lambda: self._player.update_status(status))

    @Slot(int)
    def _on_position(self, ms: int):
        if self._player:
            self._schedule(lambda: self._player.update_position(ms))

    @Slot(int)
    def _on_volume(self, vol: int):
        if self._player:
            self._schedule(lambda: self._player.update_volume(vol))

    @Slot(list, int)
    def _on_queue_changed(self, queue: list, index: int):
        if self._player:
            has_next = index < len(queue) - 1
            has_prev = index > 0
            self._schedule(lambda: self._player.update_can_next_prev(has_next, has_prev))

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
