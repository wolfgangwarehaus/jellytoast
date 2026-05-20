"""Shared cast-manager primitives: the ``CastDevice`` dataclass, the
AirPlay v1 mDNS service listener, and the per-protocol settings gate.

The lazy-import availability gates (``pychromecast`` / ``zeroconf``)
and the ``run_async`` re-export deliberately live in the package
``__init__.py``, not here — see that file's module docstring for the
monkeypatch-contract rationale.
"""

import socket
from typing import Callable, Dict


from dataclasses import dataclass, field


@dataclass
class CastDevice:
    name: str
    host: str
    port: int
    # "chromecast" | "airplay" | "dlna" | "sonos" | "snapcast" — the
    # cast dialog partitions devices into one section per value.
    device_type: str
    uuid: str = ""
    # The protocol-native handle the matching transport path needs: a
    # pychromecast Chromecast, a pyatv AirPlay2Device, a DlnaDevice, a
    # SonosZone, or a SnapcastServerInfo.
    cast_object: object = field(default=None, repr=False)
    # pychromecast cast_type: "cast" (video), "audio", or "group" (a
    # multi-room speaker group). "group" unlocks per-member volume in
    # the volume popup. Non-Chromecast devices leave this at "".
    cast_type: str = ""


class _AirPlayListener:
    def __init__(self, callback: Callable):
        self.callback = callback
        self.devices: Dict[str, CastDevice] = {}

    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info and info.addresses:
            host = socket.inet_ntoa(info.addresses[0])
            display = name.replace("._airplay._tcp.local.", "").strip()
            self.devices[name] = CastDevice(
                name=display,
                host=host,
                port=info.port,
                device_type="airplay",
                uuid=name,
            )
            self.callback(list(self.devices.values()))

    def remove_service(self, zc, type_, name):
        self.devices.pop(name, None)
        self.callback(list(self.devices.values()))

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)


def _type_enabled(kind: str) -> bool:
    """Per-protocol gate: a user can disable a cast type they don't
    own so discovery skips it entirely. Reads the QSettings directly
    rather than importing modules.settings so this module stays
    light at import time (settings.py runs the legacy-org migration
    on first construction)."""
    from PySide6.QtCore import QSettings

    qs = QSettings("jellytoast", "jellytoast")
    return bool(qs.value(f"cast/{kind}_enabled", True, type=bool))
