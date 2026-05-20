"""``CastManager`` — the thin orchestrator. Composes ``_ChromecastMixin``
and ``_AirplayMixin``; owns the device caches, the active-cast slot,
the devices-changed callback, and the cross-protocol lifecycle
(discovery fanout, ``stop_cast``, ``cleanup``).
"""

from typing import Callable, List, Optional

from ._common import CastDevice
from ._chromecast import _ChromecastMixin
from ._airplay import _AirplayMixin


class CastManager(_ChromecastMixin, _AirplayMixin):
    def __init__(self):
        self.chromecast_devices: List[CastDevice] = []
        self.airplay_devices: List[CastDevice] = []
        self.active_cast: Optional[CastDevice] = None
        self._zc = None
        self._browser = None
        self._on_update: Optional[Callable] = None

    def set_devices_callback(self, cb: Callable):
        self._on_update = cb

    def _notify(self):
        if self._on_update:
            self._on_update(self.get_all_devices())

    # ── Common ──────────────────────────────────────────────────────────────

    def discover_all(self):
        self.discover_chromecasts()
        self.discover_airplay()

    def discover_all_at_boot(self):
        """Boot-time pre-warm path. Honors ``cast/discovery_timing``:
        a user on ``on_demand`` (the default) shouldn't pay the mDNS
        chatter cost just for launching the app — discovery instead
        fires when they actually open the cast menu. ``startup`` mode
        falls through to ``discover_all`` so the cast dialog opens
        with results already loaded."""
        from PySide6.QtCore import QSettings

        qs = QSettings("jellytoast", "jellytoast")
        timing = qs.value("cast/discovery_timing", "on_demand", type=str)
        if timing == "startup":
            self.discover_all()

    def get_all_devices(self) -> List[CastDevice]:
        return self.chromecast_devices + self.airplay_devices

    def stop_cast(self):
        if not self.active_cast:
            return
        if self.active_cast.device_type == "chromecast":
            self.chromecast_stop()
        else:
            self.airplay_stop()

    def cleanup(self):
        # On app exit: stop any active cast session so the receiver
        # doesn't keep playing after the controller is gone, then
        # disconnect from every known Chromecast (pychromecast holds
        # background socket threads that prevent a clean process exit
        # otherwise), then tear down zeroconf.
        try:
            self.stop_cast()
        except Exception:
            pass
        for dev in list(self.chromecast_devices):
            cc = dev.cast_object
            if cc is None:
                continue
            try:
                cc.disconnect(blocking=False)
            except Exception:
                pass
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
        # Tear down the local cast proxy's HTTP server thread, if it
        # was ever started this session.
        try:
            from modules.cast_proxy import get_cast_proxy

            get_cast_proxy().stop()
        except Exception:
            pass
