"""``CastManager`` — the thin orchestrator. Composes ``_ChromecastMixin``
and ``_AirplayMixin``; owns the device caches, the active-cast slot,
the devices-changed callback, and the cross-protocol lifecycle
(discovery fanout, ``stop_cast``, ``cleanup``).
"""

import logging
from typing import Callable, List, Optional

from ._airplay import _AirplayMixin
from ._chromecast import _ChromecastMixin
from ._common import CastDevice
from ._others import _OtherProtocolsMixin

logger = logging.getLogger(__name__)


class CastManager(_ChromecastMixin, _AirplayMixin, _OtherProtocolsMixin):
    def __init__(self):
        self.chromecast_devices: List[CastDevice] = []
        self.airplay_devices: List[CastDevice] = []
        self.dlna_devices: List[CastDevice] = []
        self.sonos_devices: List[CastDevice] = []
        self.snapcast_devices: List[CastDevice] = []
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
        """Fan discovery across all five protocols. Each ``discover_*``
        is independently gated by its own ``cast/<type>_enabled`` toggle
        (and its optional-dep probe), so calling them all unconditionally
        here is safe — a disabled or unavailable protocol no-ops."""
        self.discover_chromecasts()
        self.discover_airplay()
        self.discover_dlna()
        self.discover_sonos()
        self.discover_snapcast()

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
        return (
            self.chromecast_devices
            + self.airplay_devices
            + self.dlna_devices
            + self.sonos_devices
            + self.snapcast_devices
        )

    def stop_cast(self):
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == "chromecast":
            self.chromecast_stop()
        elif kind == "dlna":
            self.dlna_stop()
        elif kind == "sonos":
            self.sonos_stop()
        elif kind == "snapcast":
            self.snapcast_stop()
        else:
            self.airplay_stop()

    # ── Transport dispatch (mid-cast play/pause + volume + seek) ─────────
    # Route by device_type, mirroring stop_cast. Chromecast is local + fast
    # so it runs inline; DLNA/Sonos block on SOAP so they run off the GUI
    # thread. Without this, the player's transport controls reached only the
    # chromecast_* methods (early-return off-Chromecast) and silently no-op'd.

    def _run_off_thread(self, fn):
        from modules.async_io import run_async

        run_async(fn)

    def cast_toggle_pause(self):
        """Play/pause the active cast. Chromecast queries the receiver;
        DLNA/Sonos toggle a tracked flag (set False on cast start) since we
        drive their playback, so a query round-trip isn't worth it."""
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == "chromecast":
            self.chromecast_pause()
            return
        paused = getattr(self, "_cast_paused", False)
        if kind == "dlna":
            self._run_off_thread(self._dlna_resume if paused else self._dlna_pause)
        elif kind == "sonos":
            self._run_off_thread(self._sonos_resume if paused else self._sonos_pause)
        else:
            return  # AirPlay v1 / Snapcast: no transport here
        self._cast_paused = not paused

    def cast_set_volume(self, percent: int):
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == "chromecast":
            self.chromecast_set_volume(percent)
        elif kind == "dlna":
            self._run_off_thread(lambda: self._dlna_set_volume(percent))
        elif kind == "sonos":
            self._run_off_thread(lambda: self._sonos_set_volume(percent))

    def cast_seek(self, sec: float):
        """Absolute seek (the position slider). seek_relative (skip buttons)
        stays Chromecast-only for now — DLNA's controller seeks relative +
        forward-clamped, so per-backend relative handling is a follow-up."""
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == "chromecast":
            self.chromecast_seek(sec)
        elif kind == "dlna":
            self._run_off_thread(lambda: self._dlna_seek_abs(sec))
        elif kind == "sonos":
            self._run_off_thread(lambda: self._sonos_seek_abs(sec))

    def cleanup(self):
        # On app exit: stop any active cast session so the receiver
        # doesn't keep playing after the controller is gone, then
        # disconnect from every known Chromecast (pychromecast holds
        # background socket threads that prevent a clean process exit
        # otherwise), then tear down zeroconf.
        try:
            self.stop_cast()
        except Exception as e:
            # Don't swallow silently — a hung receiver on shutdown is
            # diagnostic-worthy. Print is fine here; cleanup runs once
            # per process under aboutToQuit so a logger setup would
            # be overkill.
            logger.warning("cast cleanup: stop_cast failed — %r", e)
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
        # Tear down the DLNA backend worker loop, if it was ever spun
        # up — it hosts a long-lived asyncio loop thread that would
        # otherwise keep the process alive. Soft-imported + best-effort
        # so an install without the optional dep (or a session that
        # never opened the cast menu) is unaffected.
        try:
            from modules.cast import dlna as _dlna

            _dlna.get_dlna_controller().stop()
        except Exception:
            pass
        # Tear down the local cast proxy's HTTP server thread, if it
        # was ever started this session.
        try:
            from modules.cast_proxy import get_cast_proxy

            get_cast_proxy().stop()
        except Exception:
            pass
        # Tear down the Snapcast controller's asyncio loop thread if one
        # was ever created (the control dialog lazily connects). Like the
        # DLNA backend it hosts a long-lived loop thread; leaving it
        # running races interpreter teardown. Read the module global
        # directly so cleanup doesn't *create* a controller just to stop
        # it. Best-effort + soft import.
        try:
            from modules.cast import snapcast as _snap

            if _snap._CONTROLLER is not None:
                _snap._CONTROLLER.shutdown()
        except Exception:
            pass
