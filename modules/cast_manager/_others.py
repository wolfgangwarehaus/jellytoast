"""``_OtherProtocolsMixin`` — discovery fan-out + stop routing for the
three backend-module cast protocols: DLNA, Sonos, Snapcast.

Each ``discover_<type>`` gates on the per-type ``cast/<type>_enabled``
toggle, no-ops gracefully when the backend's optional dependency is
absent, runs the backend's (blocking) discovery off the GUI thread via
the package-level ``run_async`` (resolved at call time so the
``test_cast_gating`` monkeypatch contract holds), adapts the result
into ``CastDevice`` records, and pushes them through ``_notify`` so the
cast dialog's per-protocol sections fill via the same path Chromecast
and AirPlay use.

Transport for these three protocols stays in the backend modules under
``modules/cast/``; this mixin only wires discovery and stop routing.
"""

from typing import List

from ._common import CastDevice, _type_enabled


class _OtherProtocolsMixin:
    # ── DLNA ─────────────────────────────────────────────────────────

    def discover_dlna(self):
        """Discover DLNA / UPnP-AV renderers via SSDP M-SEARCH.

        Gated by ``cast/dlna_enabled`` and the optional
        ``async-upnp-client`` dependency. The backend's ``discover`` is
        a blocking SSDP sweep, so it runs on the shared thread pool —
        the GUI thread never stalls. Results are adapted into
        ``CastDevice`` rows (``device_type="dlna"``) carrying the
        backend ``DlnaDevice`` in ``cast_object``."""
        if not _type_enabled("dlna"):
            return
        from modules import cast_manager as _pkg

        try:
            from modules.cast import dlna as _dlna
        except Exception as e:
            print(f"DLNA discovery prep failed: {e}")
            return
        if not _dlna.is_available():
            return

        def _go() -> List[CastDevice]:
            controller = _dlna.get_dlna_controller()
            found = controller.discover(timeout=5)
            return [
                CastDevice(
                    name=d.name,
                    host=d.host,
                    port=d.port,
                    device_type="dlna",
                    uuid=d.udn,
                    cast_object=d,
                )
                for d in found
            ]

        def _on_result(devices: List[CastDevice]) -> None:
            self.dlna_devices = devices
            self._notify()

        _pkg.run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: print(f"DLNA discovery: {e}"),
        )

    # ── Sonos ────────────────────────────────────────────────────────

    def discover_sonos(self):
        """Discover Sonos zones via SSDP. Gated by ``cast/sonos_enabled``
        and the optional ``soco`` dependency. ``discover_sonos`` is a
        blocking SSDP sweep so it runs off the GUI thread; each zone
        becomes a ``CastDevice`` (``device_type="sonos"``) carrying the
        ``SonosZone`` in ``cast_object``."""
        if not _type_enabled("sonos"):
            return
        from modules import cast_manager as _pkg

        try:
            from modules.cast import sonos as _sonos
        except Exception as e:
            print(f"Sonos discovery prep failed: {e}")
            return
        if not _sonos.is_available():
            return

        def _go() -> List[CastDevice]:
            zones = _sonos.discover_sonos(timeout=1.0)
            return [
                CastDevice(
                    name=z.label,
                    host=z.coordinator_ip,
                    port=0,  # soco drives SOAP ports internally
                    device_type="sonos",
                    uuid=z.uuid,
                    cast_object=z,
                    cast_type="group" if z.is_group else "",
                )
                for z in zones
            ]

        def _on_result(devices: List[CastDevice]) -> None:
            self.sonos_devices = devices
            self._notify()

        _pkg.run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: print(f"Sonos discovery: {e}"),
        )

    # ── Snapcast ─────────────────────────────────────────────────────

    def discover_snapcast(self):
        """Discover Snapcast servers via mDNS. Gated by
        ``cast/snapcast_enabled`` and the optional ``snapcast`` package.

        The backend's ``discover_servers`` already owns its own daemon
        thread (a documented exception to the async_io rule) and fires
        ``on_result`` once the browse window closes; we adapt that
        callback directly rather than wrapping it in another
        ``run_async``. Each server becomes a ``CastDevice``
        (``device_type="snapcast"``) carrying the ``SnapcastServerInfo``
        in ``cast_object``."""
        if not _type_enabled("snapcast"):
            return
        try:
            from modules.cast import snapcast as _snapcast
        except Exception as e:
            print(f"Snapcast discovery prep failed: {e}")
            return
        # A discovered server is useless without the control library,
        # so gate the whole scan on it — mirrors how DLNA/Sonos gate.
        if not _snapcast._ensure_snapcast():
            return

        def _on_result(servers) -> None:
            self.snapcast_devices = [
                CastDevice(
                    name=s.hostname or s.host,
                    host=s.host,
                    port=s.port,
                    device_type="snapcast",
                    uuid=f"{s.host}:{s.port}",
                    cast_object=s,
                )
                for s in servers
            ]
            self._notify()

        try:
            _snapcast.discover_servers(_on_result)
        except Exception as e:
            print(f"Snapcast discovery: {e}")

    # ── Stop routing ─────────────────────────────────────────────────

    def dlna_stop(self):
        """Stop the active DLNA renderer. Delegates to the backend
        ``DlnaController`` — transport lives there, not here."""
        try:
            from modules.cast import dlna as _dlna

            _dlna.get_dlna_controller().stop_renderer()
        except Exception as e:
            print(f"DLNA stop: {e}")
        self.active_cast = None

    def sonos_stop(self):
        """Stop the active Sonos zone. The ``SonosZone`` carried in
        ``cast_object`` is what the backend's ``stop_sonos`` resolves to
        a coordinator."""
        try:
            from modules.cast import sonos as _sonos

            if self.active_cast is not None:
                _sonos.stop_sonos(self.active_cast.cast_object)
        except Exception as e:
            print(f"Sonos stop: {e}")
        self.active_cast = None

    def snapcast_stop(self):
        """Snapcast has no single 'stop' verb — it's a routing matrix,
        not a player. Clearing the active-cast pointer is the most we
        can do generically; the snapcast popup drives group/stream
        changes through ``SnapcastController`` directly."""
        self.active_cast = None
