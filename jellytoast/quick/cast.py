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


class Cast(_CastDispatcherMixin, QObject):
    changed = Signal()
    failed = Signal(str)  # message for the UI
    _devices_found = Signal(list)  # discovery callback → GUI thread

    SCAN_MS = 8000

    def __init__(self, cast_manager=None, parent=None):
        QObject.__init__(self, parent)
        self.cast_manager = cast_manager
        self.bus = PlayerBus.get()
        self._devices: List = []
        self._scanning = False
        self._active = ""
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(self.SCAN_MS)
        self._scan_timer.timeout.connect(self._scan_done)
        self._devices_found.connect(self._on_devices)
        self.bus.cast_started.connect(self._on_started)
        self.bus.cast_stopped.connect(self._on_stopped)
        active = getattr(cast_manager, "active_cast", None)
        self._active = getattr(active, "name", "") or ""

    # ── state ──
    def _get_devices(self) -> List[Dict]:
        return [
            {
                "name": d.name,
                "uuid": d.uuid or d.name,
                "type": str(getattr(d.device_type, "value", d.device_type)),
            }
            for d in self._devices
        ]

    available = Property(bool, lambda self: self.cast_manager is not None, constant=True)
    devices = Property("QVariantList", _get_devices, notify=changed)
    scanning = Property(bool, lambda self: self._scanning, notify=changed)
    active = Property(str, lambda self: self._active, notify=changed)

    def _enabled(self) -> bool:
        from jellytoast.settings import get_settings

        return bool(get_settings().any_cast_type_enabled)

    enabled = Property(bool, _enabled, notify=changed)

    def _on_devices(self, devices: list) -> None:
        self._devices = list(devices or [])
        self.changed.emit()

    def _on_started(self, name: str) -> None:
        self._active = name
        self.changed.emit()

    def _on_stopped(self) -> None:
        self._active = ""
        self.changed.emit()

    def _scan_done(self) -> None:
        self._scanning = False
        self.changed.emit()

    # ── actions ──
    @Slot()
    def scan(self) -> None:
        if self.cast_manager is None:
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
        self._scan_timer.stop()
        self._scanning = False
        self.changed.emit()

    @Slot(str)
    def castTo(self, uuid: str) -> None:  # noqa: N802
        dev = next((d for d in self._devices if (d.uuid or d.name) == uuid), None)
        if dev is None:
            return
        self._cast_to_device(dev)

    @Slot()
    def disconnect(self) -> None:
        if self.cast_manager is not None:
            self.cast_manager.stop_cast()

    # ── the two Widgets-dialog hooks ──
    def _cast_failed_ui(self, dev) -> None:
        self.failed.emit(_cast_failure_text(self.cast_manager, dev))

    def _run_airplay_pairing(self, ap2_dev) -> str:
        self.failed.emit(
            self.tr("Pair {0} once from the desktop jellytoast app, then cast from here.").format(
                ap2_dev.name
            )
        )
        return ""
