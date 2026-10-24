"""Cast picker for the Qt Quick front end.

Discovery, the per-protocol hand-off and resume-on-failure are the Widgets
app's own code (``CastManager`` + ``_CastDispatcherMixin._cast_to_device``);
this only swaps the two places that would open a Widgets dialog: a failed
cast becomes a message here, and AirPlay 2 pairing is left to the desktop
app."""

from __future__ import annotations

import logging
from typing import Dict, List

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from jellytoast.cast_dispatcher import _cast_failure_text, _CastDispatcherMixin
from jellytoast.player_state import PlayerBus

logger = logging.getLogger(__name__)


def _kind(dev) -> str:
    """How a device is drawn: ``group`` (a multi-room speaker group),
    ``display`` (a TV / screen) or ``speaker``."""
    cast_type = getattr(dev, "cast_type", "") or ""
    if cast_type == "group":
        return "group"
    if cast_type == "cast":  # a Chromecast with a screen
        return "display"
    if str(getattr(dev.device_type, "value", dev.device_type)) == "dlna":
        return "display"  # DLNA renderers are mostly TVs
    return "speaker"


class Cast(_CastDispatcherMixin, QObject):
    changed = Signal()  # scanning / active / enabled / available / connecting
    devicesChanged = Signal()  # only when the device set changes (rebuilds rows)
    volumeChanged = Signal()
    membersChanged = Signal()
    failed = Signal(str)  # message for the UI
    _devices_found = Signal(list)  # discovery callback → GUI thread

    SCAN_MS = 8000
    CONNECT_MS = 25000  # give up showing "connecting" after this

    def __init__(self, cast_manager=None, parent=None):
        QObject.__init__(self, parent)
        self.cast_manager = cast_manager
        self.bus = PlayerBus.get()
        self._devices: List = []
        self._scanning = False
        self._active = ""
        self._connecting = ""  # uuid of the device a cast is starting to
        self._scan_wanted = False  # scan() before the core attached
        self._volume = 0
        self._members: List[Dict] = []
        self._members_loading = False
        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.setInterval(self.CONNECT_MS)
        self._connect_timer.timeout.connect(self._connect_gave_up)
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(self.SCAN_MS)
        self._scan_timer.timeout.connect(self._scan_done)
        self._devices_found.connect(self._on_devices)
        self.bus.cast_started.connect(self._on_started)
        self.bus.cast_stopped.connect(self._on_stopped)
        self.bus.volume_state.connect(self._on_volume)
        active = getattr(cast_manager, "active_cast", None)
        self._active = getattr(active, "name", "") or ""

    # ── state ──
    def _get_devices(self) -> List[Dict]:
        return [
            {
                "name": d.name,
                "uuid": d.uuid or d.name,
                "type": str(getattr(d.device_type, "value", d.device_type)),
                "kind": _kind(d),
            }
            for d in self._devices
        ]

    available = Property(bool, lambda self: self.cast_manager is not None, notify=changed)

    def attach(self, cast_manager) -> None:
        """Pick up the cast manager once the core is running."""
        self.cast_manager = cast_manager
        active = getattr(cast_manager, "active_cast", None)
        self._active = getattr(active, "name", "") or ""
        self.changed.emit()
        if self._scan_wanted:  # the sheet opened before the core was up
            self._scan_wanted = False
            self.scan()

    devices = Property("QVariantList", _get_devices, notify=devicesChanged)
    scanning = Property(bool, lambda self: self._scanning, notify=changed)
    active = Property(str, lambda self: self._active, notify=changed)
    connecting = Property(str, lambda self: self._connecting, notify=changed)

    def _active_dev(self):
        return getattr(self.cast_manager, "active_cast", None)

    def _get_active_kind(self) -> str:
        dev = self._active_dev()
        return _kind(dev) if dev is not None and self._active else ""

    activeKind = Property(str, _get_active_kind, notify=changed)
    # Whether the active device's volume can be set from here (AirPlay v1
    # has no volume control).
    volumeControl = Property(
        bool,
        lambda self: self._active != ""
        and str(getattr(getattr(self._active_dev(), "device_type", ""), "value", ""))
        in ("chromecast", "dlna", "sonos"),
        notify=changed,
    )
    volume = Property(int, lambda self: self._volume, notify=volumeChanged)
    members = Property("QVariantList", lambda self: list(self._members), notify=membersChanged)
    membersLoading = Property(bool, lambda self: self._members_loading, notify=membersChanged)

    def _enabled(self) -> bool:
        from jellytoast.settings import get_settings

        return bool(get_settings().any_cast_type_enabled)

    enabled = Property(bool, _enabled, notify=changed)

    def _on_devices(self, devices: list) -> None:
        devices = list(devices or [])
        key = [(d.uuid or d.name, d.name) for d in devices]
        self._devices = devices
        # Discovery reports the full list repeatedly; only repaint (which
        # rebuilds every row) when it actually changed — rows must not move
        # under the finger.
        if key != getattr(self, "_devices_key", None):
            self._devices_key = key
            self.devicesChanged.emit()

    def _on_started(self, name: str) -> None:
        self._active = name
        self._connecting = ""
        self._connect_timer.stop()
        self._members = []
        self.membersChanged.emit()
        self.changed.emit()

    def _on_stopped(self) -> None:
        self._active = ""
        self._connecting = ""
        self._members = []
        self.membersChanged.emit()
        self.changed.emit()

    def _on_volume(self, v: int) -> None:
        # Kept even before cast_started reaches us: the player emits the
        # device's starting level from its own cast_started handler, which
        # may run first.
        if int(v) != self._volume:
            self._volume = int(v)
            self.volumeChanged.emit()

    def _connect_gave_up(self) -> None:
        if self._connecting:
            self._connecting = ""
            self.changed.emit()

    def _scan_done(self) -> None:
        self._scanning = False
        self.changed.emit()

    # ── actions ──
    @Slot()
    def scan(self) -> None:
        if self.cast_manager is None:
            self._scan_wanted = True
            self._scanning = True  # shows the spinner until the core is up
            self.changed.emit()
            return
        self.cast_manager.set_devices_callback(self._devices_found.emit)
        self._on_devices(self.cast_manager.get_all_devices())
        self._scanning = True
        self.changed.emit()
        try:
            self.cast_manager.discover_all()
        except Exception as e:
            logger.warning("cast discovery failed: %s", e)
        self._scan_timer.start()

    @Slot()
    def stopScan(self) -> None:  # noqa: N802
        if (
            self.cast_manager is not None
            and self.cast_manager._on_update == self._devices_found.emit
        ):
            self.cast_manager.set_devices_callback(None)
        self._scan_wanted = False
        self._scan_timer.stop()
        self._scanning = False
        self.changed.emit()

    @Slot(str)
    def castTo(self, uuid: str) -> None:  # noqa: N802
        dev = next((d for d in self._devices if (d.uuid or d.name) == uuid), None)
        if dev is None or dev.name == self._active:
            return
        self._connecting = uuid
        self._connect_timer.start()
        self.changed.emit()
        self._cast_to_device(dev)

    @Slot(int)
    def setVolume(self, v: int) -> None:  # noqa: N802
        """The cast device's volume (the player routes it to the device
        while casting; never the phone's own level)."""
        if not self._active:
            return
        v = max(0, min(100, int(v)))
        self._volume = v
        self.volumeChanged.emit()
        self.bus.volume_changed.emit(v)

    @Slot()
    def loadMembers(self) -> None:  # noqa: N802
        """A group's speakers and their own volumes (Chromecast groups)."""
        dev = self._active_dev()
        if self.cast_manager is None or dev is None or _kind(dev) != "group":
            return
        if not hasattr(self.cast_manager, "group_members_async"):
            return
        self._members_loading = True
        self.membersChanged.emit()

        def _got(members, _name=dev.name):
            if _name != self._active:
                return
            self._members = [
                {
                    "uuid": str(m.get("uuid", "")),
                    "name": str(m.get("name", "")),
                    "volume": int(m.get("volume") or 0),
                    "available": bool(m.get("available", True)),
                }
                for m in (members or [])
            ]
            self._members_loading = False
            self.membersChanged.emit()

        self.cast_manager.group_members_async(dev, _got)

    @Slot(str, int)
    def setMemberVolume(self, uuid: str, v: int) -> None:  # noqa: N802
        if self.cast_manager is None or not self._active:
            return
        v = max(0, min(100, int(v)))
        for m in self._members:
            if m["uuid"] == uuid:
                m["volume"] = v
        self.cast_manager.set_member_volume_async(uuid, v)

    @Slot()
    def disconnect(self) -> None:
        if self.cast_manager is not None:
            self.cast_manager.stop_cast()

    # ── the two Widgets-dialog hooks ──
    def _cast_failed_ui(self, dev) -> None:
        self._connecting = ""
        self._connect_timer.stop()
        self.changed.emit()
        self.failed.emit(_cast_failure_text(self.cast_manager, dev))

    def _run_airplay_pairing(self, ap2_dev) -> str:
        self._connecting = ""
        self._connect_timer.stop()
        self.changed.emit()
        self.failed.emit(
            self.tr("Pair {0} once from the desktop jellytoast app, then cast from here.").format(
                ap2_dev.name
            )
        )
        return ""
