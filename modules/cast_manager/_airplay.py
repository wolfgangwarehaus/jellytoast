"""AirPlay discovery + transport: the legacy v1 mDNS path and the
modern pyatv (AirPlay 1/2) path. Mixed into ``CastManager`` via
``_AirplayMixin``.

Monkeypatch indirection: the test suite patches ``Zeroconf``,
``ServiceBrowser`` and ``run_async`` on the ``modules.cast_manager``
package namespace. This module resolves those symbols through the
package (``from modules import cast_manager as _pkg``) at call time so
a test patch is honoured — a direct import would freeze a stale
reference.
"""

import logging
from typing import List

from ._common import CastDevice, _AirPlayListener, _type_enabled

logger = logging.getLogger(__name__)


class _AirplayMixin:
    # ── AirPlay (v1 mDNS fallback + pyatv v1/v2 path) ──────────────────────

    def discover_airplay(self):
        from modules import cast_manager as _pkg

        if not _type_enabled("airplay"):
            return
        # Prefer pyatv-based discovery when the library is installed —
        # it reports both AirPlay 1 and AirPlay 2 receivers and tells
        # us which need pairing. Fall back to the lightweight zeroconf
        # ServiceBrowser path (AirPlay 1 only) if pyatv isn't around.
        try:
            from modules import airplay2 as _ap2

            if _ap2.is_available():
                self._discover_airplay_pyatv()
                return
        except Exception as e:
            logger.warning("AirPlay 2 discovery prep failed: %s", e)
        if not _pkg._ensure_zeroconf():
            return

        def _go():
            zc = _pkg.Zeroconf()
            listener = _AirPlayListener(
                lambda d: setattr(self, "airplay_devices", d) or self._notify()
            )
            browser = _pkg.ServiceBrowser(zc, "_airplay._tcp.local.", listener)
            return zc, browser

        def _on_result(pair) -> None:
            self._zc, self._browser = pair

        _pkg.run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: logger.warning("AirPlay discovery: %s", e),
        )

    def _discover_airplay_pyatv(self):
        """Streaming AirPlay scan via pyatv. Runs on the shared pool —
        pyatv.scan blocks for its timeout. The result list is
        translated into ``CastDevice`` entries with the pyatv config
        stuffed into ``cast_object`` so ``cast_to_airplay`` can route
        the cast through pyatv's AirPlay 2 client."""
        from modules import airplay2 as _ap2
        from modules import cast_manager as _pkg

        def _go() -> List[CastDevice]:
            ap2_devices = _ap2.scan_sync(timeout=3.0)
            return [
                CastDevice(
                    name=d.name,
                    host=d.host,
                    port=0,  # pyatv handles ports internally
                    device_type="airplay",
                    uuid=d.identifier,
                    cast_object=d,  # carries pyatv config + pairing flag
                )
                for d in ap2_devices
            ]

        def _on_result(devices: List[CastDevice]) -> None:
            self.airplay_devices = devices
            self._notify()

        _pkg.run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: logger.warning("AirPlay 2 discovery: %s", e),
        )

    def cast_to_airplay(self, dev: CastDevice, url: str, title: str = "") -> bool:
        # Route through pyatv (AirPlay 2) when the device was discovered
        # via pyatv — that path handles paired credentials, encrypted
        # control, and RTSP streaming. ``cast_object`` is the
        # ``AirPlay2Device`` returned by ``modules.airplay2.scan_sync``.
        from modules import airplay2 as _ap2

        # Same proxy routing as the Chromecast path — an AirPlay
        # receiver can't reach a Tailscale / remote server URL either.
        from modules.cast_proxy import resolve_cast_url

        url = resolve_cast_url(url)
        logger.debug(
            "cast_to_airplay: dev=%r cast_object_type=%s",
            dev.name,
            type(dev.cast_object).__name__,
        )
        if isinstance(dev.cast_object, _ap2.AirPlay2Device):
            return self._cast_to_airplay2(dev, url)
        # Legacy AirPlay 1 path — simple HTTP POST. Still useful for
        # ALAC speakers / older Apple TVs that pre-date AirPlay 2.
        try:
            import http.client

            body = f"Content-Location: {url}\nStart-Position: 0\n"
            conn = http.client.HTTPConnection(dev.host, dev.port, timeout=5)
            conn.request(
                "POST",
                "/play",
                body=body.encode(),
                headers={
                    "Content-Type": "text/parameters",
                    "X-Apple-Session-ID": "1",
                    "User-Agent": "MediaControl/1.0",
                },
            )
            resp = conn.getresponse()
            conn.close()
            if resp.status in (200, 201):
                self.active_cast = dev
                return True
            return False
        except Exception as e:
            logger.warning("AirPlay cast: %s", e)
            return False

    def _cast_to_airplay2(self, dev: CastDevice, url: str) -> bool:
        """Hand off to pyatv on a worker thread. ``cast_to_airplay``'s
        sync contract is preserved by blocking on the worker's
        completion via a one-shot QEventLoop. That keeps the existing
        callers (which expect a True/False return) happy without
        moving the cast button into an async story."""
        from PySide6.QtCore import QEventLoop

        from modules import airplay2 as _ap2
        from modules import cast_manager as _pkg

        ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
        logger.debug(
            "_cast_to_airplay2: dev=%r id=%r url_len=%s",
            ap2_dev.name,
            ap2_dev.identifier,
            len(url),
        )

        result = {"ok": False, "err": None}
        loop = QEventLoop()

        def _go() -> bool:
            logger.debug("worker: calling play_url_sync")
            _ap2.play_url_sync(ap2_dev, url)
            logger.debug("worker: play_url_sync returned")
            return True

        def _on_result(_):
            result["ok"] = True
            loop.quit()

        def _on_error(e):
            logger.debug(
                "worker on_error: %s: %s", type(e).__name__, e
            )
            result["err"] = e
            loop.quit()

        _pkg.run_async(_go, on_result=_on_result, on_error=_on_error)
        loop.exec()  # blocks until worker completes
        if result["ok"]:
            logger.debug("_cast_to_airplay2: success for %r", dev.name)
            self.active_cast = dev
            return True
        err = result["err"]
        if isinstance(err, _ap2.PairingRequired):
            # Tag a flag the cast dialog can pick up to launch the
            # pairing UI. For now we just log and return False —
            # the pairing dialog ships in a follow-up.
            logger.warning("AirPlay 2 cast: pairing required for %s", dev.name)
        else:
            logger.warning(
                "AirPlay 2 cast: %s: %s", type(err).__name__, err
            )
            import traceback

            traceback.print_exception(type(err), err, err.__traceback__)
        return False

    def airplay_stop(self):
        if self.active_cast and self.active_cast.device_type == "airplay":
            from modules import airplay2 as _ap2

            if isinstance(self.active_cast.cast_object, _ap2.AirPlay2Device):
                # pyatv has no explicit "stop" on the AirPlay 2 stream
                # API — the receiver halts when the streamer drops.
                # Closing the active connection happens inside
                # play_url_sync's finally, so there's nothing to do
                # here. Future enhancement: persistent AirPlay 2
                # session with explicit pause/stop control.
                pass
            else:
                try:
                    import http.client

                    conn = http.client.HTTPConnection(
                        self.active_cast.host, self.active_cast.port, timeout=3
                    )
                    conn.request("POST", "/stop", headers={"X-Apple-Session-ID": "1"})
                    conn.getresponse()
                    conn.close()
                except Exception:
                    pass
        self.active_cast = None
