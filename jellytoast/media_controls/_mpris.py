"""MPRIS2 D-Bus media player interface — the Linux backend for
jellytoast.media_controls.

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
import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, Slot

logger = logging.getLogger(__name__)

# This module is only imported when jellytoast.platform_compat.IS_LINUX is
# True (see jellytoast/media_controls/__init__.py). On Linux without
# dbus-next installed the import below raises and the package falls
# back to the unsupported backend.
from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess, RequestNameReply
from dbus_next.service import ServiceInterface, dbus_property, method, signal

from jellytoast.player_state import NowPlaying, PlayerBus, get_now_playing
from jellytoast.settings import get_settings

SERVICE_NAME = "org.mpris.MediaPlayer2.jellytoast"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


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
        return "jellytoast"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return "jellytoast"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return ["http", "https"]

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return [
            "audio/mpeg",
            "audio/flac",
            "audio/x-flac",
            "audio/ogg",
            "audio/x-vorbis+ogg",
            "audio/aac",
            "audio/x-aac",
            "audio/mp4",
            "audio/wav",
            "video/mp4",
            "video/x-matroska",
            "video/webm",
            "application/x-mpegURL",
        ]


class MprisPlayer(ServiceInterface):
    """org.mpris.MediaPlayer2.Player — playback interface."""

    def __init__(self, bus: PlayerBus):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._bus = bus
        self._status = "Stopped"  # Playing | Paused | Stopped
        # Seed loop/shuffle from the persisted state — QueueManager
        # boots with the same settings, so a False/"None" default here
        # shows playerctl/Plasma a stale "off" until the user toggles.
        _s = get_settings()
        self._loop = {"off": "None", "one": "Track", "all": "Playlist"}.get(
            (_s.repeat_mode or "off"), "None"
        )
        self._shuffle = bool(_s.shuffle)
        self._volume = _s.volume / 100.0
        self._position = 0  # microseconds
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
        # MPRIS2 spec: no-op unless track_id matches the current track's
        # mpris:trackid. Guards against a client seeking a track that has
        # since auto-advanced — without this it would jump the now-current
        # track to the stale track's offset. Empty metadata → "/" never
        # matches a real object path.
        current = self._metadata.get("mpris:trackid")
        current_id = current.value if isinstance(current, Variant) else "/"
        if current_id != track_id:
            return
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
        # D-Bus object-path elements only accept [A-Za-z0-9_] — anything
        # else (the dash in user-added "local-XXXXXXXX" radio ids, for
        # one) makes dbus-next throw SignatureBodyMismatchError on every
        # metadata refresh. Map invalid chars to underscore.
        if np.item_id:
            safe_id = "".join(
                c if (c.isalnum() or c == "_") else "_" for c in np.item_id
            )
            track_id = f"/org/jellytoast/track/{safe_id}"
        else:
            track_id = "/"
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

    def update_shuffle(self, on: bool):
        # Push an app-side shuffle change out to D-Bus so MPRIS clients
        # (Plasma / GNOME / playerctl) don't show a stale Shuffle. Setting
        # _shuffle is idempotent, so a re-entry from an MPRIS-originated
        # change (Shuffle.setter → bus → here) is harmless.
        self._shuffle = bool(on)
        self.emit_properties_changed({"Shuffle": self._shuffle})

    def update_loop_status(self, mode: str):
        # App repeat mode ('off'|'one'|'all') → MPRIS LoopStatus; mirror of
        # the LoopStatus.setter mapping. Idempotent re-entry like update_shuffle.
        self._loop = {"off": "None", "one": "Track", "all": "Playlist"}.get(mode, "None")
        self.emit_properties_changed({"LoopStatus": self._loop})

    def update_can_next_prev(self, has_next: bool, has_prev: bool):
        self._can_go_next = has_next
        self._can_go_prev = has_prev
        self.emit_properties_changed(
            {
                "CanGoNext": has_next,
                "CanGoPrevious": has_prev,
            }
        )

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
        # Cached queue shape + repeat mode so CanGoNext/CanGoPrevious can
        # be repeat-aware (queue_can_next_prev) — either signal firing
        # recomputes from the pair.
        self._queue_len = 0
        self._queue_index = -1
        self._repeat_mode = "off"

    def start(self, window=None):
        # Sanctioned raw-thread exception (see cast/dlna/_loop.py): this
        # hosts a long-lived D-Bus asyncio loop, not a blocking one-shot —
        # it can't be a pool job.
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait briefly so the common case starts fully wired, but connect
        # the Qt signals EITHER WAY: every slot already guards on
        # ``self._player``/``self._loop``, so pre-ready emissions no-op —
        # whereas skipping the connects on a slow session bus (>3 s) left
        # MPRIS permanently dead even after setup eventually succeeded.
        self._ready.wait(timeout=3.0)
        self._connect_signals()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._setup())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:
            logger.warning("MPRIS error: %s", e)
            self._ready.set()

    async def _setup(self):
        self._dbus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._root = MprisRoot(self.bus)
        self._player = MprisPlayer(self.bus)
        self._dbus.export(OBJECT_PATH, self._root)
        self._dbus.export(OBJECT_PATH, self._player)
        reply = await self._dbus.request_name(SERVICE_NAME)
        # Don't silently register as a queued non-primary owner: if another
        # process already holds the well-known name, media keys / the Plasma
        # widget keep talking to IT while we think MPRIS is up. Surface it.
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            logger.warning(
                "MPRIS: did not get primary ownership of %s (reply=%s) — "
                "another owner holds it; media controls may not reach us",
                SERVICE_NAME,
                getattr(reply, "name", reply),
            )

    # ── Forward Qt signals to dbus-next via call_soon_threadsafe ────────────

    def _connect_signals(self):
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_paused.connect(lambda: self._update_status("Paused"))
        self.bus.playback_resumed.connect(lambda: self._update_status("Playing"))
        self.bus.playback_stopped.connect(lambda: self._update_status("Stopped"))
        self.bus.position_updated.connect(self._on_position)
        self.bus.volume_state.connect(self._on_volume)
        self.bus.queue_changed.connect(self._on_queue_changed)
        self.bus.seek_requested.connect(self._on_seek_requested)
        # Push app-side shuffle / repeat changes out to D-Bus (without these,
        # MPRIS Shuffle / LoopStatus go stale the moment the user toggles them
        # in-app — only client-originated changes ever updated D-Bus).
        self.bus.shuffle_changed.connect(self._on_shuffle)
        self.bus.repeat_changed.connect(self._on_repeat)

    def _schedule(self, fn):
        # fn must be a zero-arg callable (NOT a coroutine): it is handed to
        # call_soon_threadsafe, which only runs plain callbacks — a coroutine
        # would be silently dropped.
        if self._loop and self._player:
            self._loop.call_soon_threadsafe(fn)

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
    def _on_seek_requested(self, ms: int):
        # Announce a discontinuous position change to MPRIS clients
        # (Plasma / GNOME Shell / playerctl) via the spec-required Seeked
        # signal — many clients only re-read position on Seeked, so without
        # this their scrubbers freeze at the pre-seek spot. Fires on any
        # absolute seek (slider drag, SetPosition, resume-to-position).
        if self._player:
            self._schedule(lambda: self._player.emit_seeked(ms))

    @Slot(int)
    def _on_volume(self, vol: int):
        if self._player:
            self._schedule(lambda: self._player.update_volume(vol))

    @Slot(bool)
    def _on_shuffle(self, on: bool):
        if self._player:
            self._schedule(lambda: self._player.update_shuffle(on))

    @Slot(str)
    def _on_repeat(self, mode: str):
        self._repeat_mode = mode
        if self._player:
            self._schedule(lambda: self._player.update_loop_status(mode))
            # Repeat affects CanGoNext/CanGoPrevious too (repeat-all wraps),
            # so recompute against the cached queue shape.
            self._push_can_next_prev()

    @Slot(list, int)
    def _on_queue_changed(self, queue: list, index: int):
        self._queue_len = len(queue)
        self._queue_index = index
        self._push_can_next_prev()

    def _push_can_next_prev(self):
        if not self._player:
            return
        from jellytoast.player_state import queue_can_next_prev

        has_next, has_prev = queue_can_next_prev(
            self._queue_len, self._queue_index, self._repeat_mode
        )
        self._schedule(lambda: self._player.update_can_next_prev(has_next, has_prev))

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
