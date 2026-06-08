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

import logging
from typing import List

from ._common import CastDevice, CastType, _type_enabled

logger = logging.getLogger(__name__)


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
            logger.warning("DLNA discovery prep failed: %s", e)
            return
        if not _dlna.is_available():
            return

        def _go() -> List[CastDevice]:
            controller = _dlna.get_dlna_controller()
            # validate=True drops SSDP responders that aren't a real
            # bindable DMR (so a non-renderer box doesn't clutter the
            # picker + fail at cast time) and resolves friendly names, so
            # the row reads "Living Room TV" instead of a bare IP.
            found = controller.discover(timeout=5, validate=True)
            return [
                CastDevice(
                    name=d.name,
                    host=d.host,
                    port=d.port,
                    device_type=CastType.DLNA,
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
            on_error=lambda e: logger.warning("DLNA discovery: %s", e),
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
            logger.warning("Sonos discovery prep failed: %s", e)
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
                    device_type=CastType.SONOS,
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
            on_error=lambda e: logger.warning("Sonos discovery: %s", e),
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
            logger.warning("Snapcast discovery prep failed: %s", e)
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
                    device_type=CastType.SNAPCAST,
                    uuid=f"{s.host}:{s.port}",
                    cast_object=s,
                )
                for s in servers
            ]
            self._notify()

        try:
            _snapcast.discover_servers(_on_result)
        except Exception as e:
            logger.warning("Snapcast discovery: %s", e)

    # ── Play routing (URL-push backends) ─────────────────────────────
    #
    # DLNA and Sonos are URL-push protocols: the receiver fetches a
    # (cast-proxy-resolved) stream URL and plays it, exactly like the
    # Chromecast / AirPlay mixins. These two methods mirror
    # ``cast_to_chromecast`` / ``cast_to_airplay`` — blocking, return a
    # bool, set ``active_cast`` on success — so the two dispatch sites
    # (``_cast_to_device`` and ``MpvController.play``) treat all four
    # the same way. They block on SOAP/UPnP (DLNA up to 30 s), so call
    # them off the GUI thread. Snapcast is a control surface, not a URL
    # push, so it has no ``cast_to_*`` here.

    def cast_to_dlna(
        self, dev, stream_url, meta, *, transcode_url_fn=None, force_transcode=False, start_sec=0.0
    ) -> bool:
        """Push the current track to a DLNA renderer. ``dev.cast_object``
        is the ``DlnaDevice``; ``meta`` is a ``TrackMetadata``. The
        controller requires a cast-proxy URL, so resolve it here (the
        controller's contract puts that on the caller, same as the
        Chromecast / AirPlay 2 sites). ``start_sec`` resumes a mid-track
        cast at the right offset. On success, start the controller's 1 s
        transport poll so the now-playing bar can advance (the player
        backend reads ``last_state()`` off the cast-poll tick)."""
        try:
            from modules.cast import dlna as _dlna
            from modules.cast_proxy import resolve_cast_url
        except Exception as e:
            logger.warning("DLNA cast prep failed: %s", e)
            return False
        if not _dlna.is_available():
            return False
        url = resolve_cast_url(stream_url) if stream_url else stream_url
        controller = _dlna.get_dlna_controller()
        try:
            ok = controller.play(
                dev.cast_object,
                url,
                meta,
                transcode_url_fn=transcode_url_fn,
                force_transcode=force_transcode,
                start_sec=start_sec,
            )
        except Exception as e:
            logger.warning("DLNA cast: %s", e)
            return False
        if ok:
            self._arm_active_cast(dev, reset_paused=True)  # fresh cast starts playing
            try:
                controller.start_polling()
            except Exception as e:
                logger.warning("DLNA start_polling: %s", e)
        return bool(ok)

    def cast_to_sonos(self, dev, url, *, title="", artist="", album="", art_url="") -> bool:
        """Push the current track to a Sonos zone's coordinator with DIDL
        metadata. ``dev.cast_object`` is the ``SonosZone``. The backend
        ``cast_to_sonos`` routes ``url`` through the cast proxy itself."""
        try:
            from modules.cast import sonos as _sonos
        except Exception as e:
            logger.warning("Sonos cast prep failed: %s", e)
            return False
        if not _sonos.is_available():
            return False
        try:
            ok = _sonos.cast_to_sonos(
                dev.cast_object,
                url,
                title=title,
                artist=artist,
                album=album,
                art_url=art_url,
            )
        except Exception as e:
            logger.warning("Sonos cast: %s", e)
            return False
        if ok:
            self._arm_active_cast(dev, reset_paused=True)  # fresh cast starts playing
        return bool(ok)

    # ── Stop routing ─────────────────────────────────────────────────

    def dlna_stop(self):
        """Stop the active DLNA renderer. Delegates to the backend
        ``DlnaController`` — transport lives there, not here. Also stop
        the transport poll started by ``cast_to_dlna``."""
        try:
            from modules.cast import dlna as _dlna

            controller = _dlna.get_dlna_controller()
            controller.stop_polling()
            controller.stop_renderer()
        except Exception as e:
            logger.warning("DLNA stop: %s", e)
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
            logger.warning("Sonos stop: %s", e)
        self.active_cast = None

    def snapcast_stop(self):
        """Snapcast has no single 'stop' verb — it's a routing matrix,
        not a player. Clearing the active-cast pointer is the most we
        can do generically; the snapcast popup drives group/stream
        changes through ``SnapcastController`` directly."""
        self.active_cast = None

    # ── Transport control (DLNA + Sonos) ─────────────────────────────
    # The transport surface lives in the backends (DlnaController.pause/
    # resume/seek/set_volume; modules.cast.sonos.pause_sonos/play_sonos/
    # seek_sonos/set_volume); these thin wrappers + the cast_* dispatchers
    # in _manager wire them to the player's controls, which previously
    # reached chromecast-only methods and silently no-op'd off-Chromecast.
    # All block on SOAP, so the dispatcher runs them off the GUI thread.
    # Sonos is UNVERIFIED against real hardware (no device available).

    def _dlna_pause(self):
        from modules.cast import dlna as _dlna

        return _dlna.get_dlna_controller().pause()

    def _dlna_resume(self):
        from modules.cast import dlna as _dlna

        return _dlna.get_dlna_controller().resume()

    def _dlna_set_volume(self, percent: int):
        from modules.cast import dlna as _dlna

        _dlna.get_dlna_controller().set_volume(int(percent))

    def _dlna_seek_abs(self, abs_sec: float):
        # controller.seek() uses UPnP REL_TIME, which is position FROM TRACK
        # START (absolute within the track), not a delta — so pass the target
        # straight through. Seeking to an earlier position works fine.
        from modules.cast import dlna as _dlna

        _dlna.get_dlna_controller().seek(max(0.0, float(abs_sec)))

    def _dlna_seek_relative(self, delta_sec: float):
        # Skip ±: derive the absolute target from the last polled position,
        # then absolute-seek (REL_TIME). Forward + backward both work.
        from modules.cast import dlna as _dlna

        c = _dlna.get_dlna_controller()
        pos = float((c.last_state() or {}).get("position_sec") or 0.0)
        c.seek(max(0.0, pos + float(delta_sec)))

    def _sonos_pause(self):
        from modules.cast import sonos as _sonos

        if self.active_cast is None:
            return False
        return _sonos.pause_sonos(self.active_cast.cast_object)

    def _sonos_resume(self):
        from modules.cast import sonos as _sonos

        if self.active_cast is None:
            return False
        return _sonos.play_sonos(self.active_cast.cast_object)

    def _sonos_set_volume(self, percent: int):
        from modules.cast import sonos as _sonos

        if self.active_cast is not None:
            _sonos.set_volume(self.active_cast.cast_object, int(percent))

    def _sonos_initial_volume(self, percent: int) -> int:
        """Clamp the connect-time cast volume up to the user's Sonos
        volume floor, so the initial push never drops a zone below the
        configured audible minimum. The floor is otherwise applied only
        inside ``cast_to_sonos`` (``_apply_volume_floor``); a raw
        ``set_volume`` here would bypass it. Falls back to ``percent``
        if settings are unavailable."""
        try:
            from modules.settings import get_settings

            floor = int(get_settings().sonos_volume_floor)
        except Exception:
            floor = 0
        return max(int(percent), floor)

    def _sonos_seek_abs(self, abs_sec: float):
        from modules.cast import sonos as _sonos

        if self.active_cast is not None:
            _sonos.seek_sonos(self.active_cast.cast_object, float(abs_sec))

    def _sonos_seek_relative(self, delta_sec: float):
        from modules.cast import sonos as _sonos

        if self.active_cast is not None:
            _sonos.seek_relative_sonos(self.active_cast.cast_object, float(delta_sec))
