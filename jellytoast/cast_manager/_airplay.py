"""AirPlay discovery + transport: the legacy v1 mDNS path and the
modern pyatv (AirPlay 1/2) path. Mixed into ``CastManager`` via
``_AirplayMixin``.

Monkeypatch indirection: the test suite patches ``Zeroconf``,
``ServiceBrowser`` and ``run_async`` on the ``jellytoast.cast_manager``
package namespace. This module resolves those symbols through the
package (``from jellytoast import cast_manager as _pkg``) at call time so
a test patch is honoured — a direct import would freeze a stale
reference.
"""

import logging
from typing import List

from ._common import CastDevice, CastType, _AirPlayListener, _type_enabled

logger = logging.getLogger(__name__)


class _AirplayMixin:
    # ── AirPlay (v1 mDNS fallback + pyatv v1/v2 path) ──────────────────────

    def discover_airplay(self):
        from jellytoast import cast_manager as _pkg

        if not _type_enabled("airplay"):
            return

        # Prefer pyatv-based discovery when the library is installed —
        # it reports both AirPlay 1 and AirPlay 2 receivers and tells
        # us which need pairing. Fall back to the lightweight zeroconf
        # ServiceBrowser path (AirPlay 1 only) if pyatv isn't around.
        # The probe runs on a pool worker: ``import pyatv`` is the
        # heaviest cold import in the app (protobuf + cryptography), and
        # on the GUI thread it froze the cast-menu open on Windows (see
        # discover_chromecasts). The branch decision marshals back to
        # the GUI thread, where both continuations find their imports
        # already cached and dispatch their own run_async sweeps.
        def _probe() -> str:
            try:
                from jellytoast import airplay2 as _ap2

                if _ap2.is_available():
                    return "pyatv"
            except Exception as e:
                logger.warning("AirPlay 2 discovery prep failed: %s", e)
            return "zeroconf" if _pkg._ensure_zeroconf() else ""

        def _dispatch(mode: str) -> None:
            if mode == "pyatv":
                self._discover_airplay_pyatv()
            elif mode == "zeroconf":
                self._discover_airplay_zeroconf()

        _pkg.run_async(
            _probe,
            on_result=_dispatch,
            on_error=lambda e: logger.warning("AirPlay discovery: %s", e),
        )

    def _discover_airplay_zeroconf(self):
        """AirPlay 1 fallback scan via a zeroconf ServiceBrowser
        (``_ensure_zeroconf`` already cached by the discover_airplay
        probe)."""
        from jellytoast import cast_manager as _pkg

        def _go():
            zc = _pkg.Zeroconf()
            listener = _AirPlayListener(
                lambda d: setattr(self, "airplay_devices", d) or self._notify()
            )
            browser = _pkg.ServiceBrowser(zc, "_airplay._tcp.local.", listener)
            return zc, browser

        def _on_result(pair) -> None:
            # Close the previous browser/zeroconf before replacing them —
            # each rescan mints a fresh Zeroconf (sockets + a listener
            # thread), so without this one leaks per rescan.
            old_browser = getattr(self, "_browser", None)
            old_zc = getattr(self, "_zc", None)
            if old_browser is not None:
                try:
                    old_browser.cancel()
                except Exception:
                    pass
            if old_zc is not None:
                try:
                    old_zc.close()
                except Exception:
                    pass
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
        from jellytoast import airplay2 as _ap2
        from jellytoast import cast_manager as _pkg

        def _go() -> List[CastDevice]:
            ap2_devices = _ap2.scan_sync(timeout=3.0)
            return [
                CastDevice(
                    name=d.name,
                    host=d.host,
                    port=0,  # pyatv handles ports internally
                    device_type=CastType.AIRPLAY,
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
        # ``AirPlay2Device`` returned by ``jellytoast.airplay2.scan_sync``.
        from jellytoast import airplay2 as _ap2

        # Same proxy routing as the Chromecast path — an AirPlay
        # receiver can't reach a Tailscale / remote server URL either.
        from jellytoast.cast_proxy import resolve_cast_url

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
            try:
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
            finally:
                # Always release the socket — request()/getresponse() can raise
                # (timeout, refused, receiver dropped) and previously leaked the
                # connection until GC.
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

        from jellytoast import airplay2 as _ap2
        from jellytoast import cast_manager as _pkg

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

        prev_active = self.active_cast
        _pkg.run_async(_go, on_result=_on_result, on_error=_on_error)
        loop.exec()  # blocks until worker completes; GUI events run meanwhile
        if result["ok"]:
            logger.debug("_cast_to_airplay2: success for %r", dev.name)
            # The nested event loop above can let the user pick ANOTHER device
            # mid-negotiation, which sets active_cast. Only claim active_cast if
            # nothing newer took it while we were blocked — otherwise we'd
            # clobber the user's later choice with this (now-stale) device.
            if self.active_cast is prev_active:
                self.active_cast = dev
            return True
        err = result["err"]
        if isinstance(err, _ap2.PairingRequired):
            # Tag a flag the cast dialog can pick up to launch the
            # pairing UI. For now we just log and return False —
            # the pairing dialog ships in a follow-up.
            logger.warning("AirPlay 2 cast: pairing required for %s", dev.name)
        else:
            # exc_info instead of a raw stderr dump: every sibling failure
            # path here logs, and on the Windows GUI-subsystem exe stderr
            # is None so print_exception was lost anyway.
            logger.warning(
                "AirPlay 2 cast: %s: %s", type(err).__name__, err, exc_info=err
            )
        return False

    def airplay_stop(self):
        if self.active_cast and self.active_cast.device_type == CastType.AIRPLAY:
            from jellytoast import airplay2 as _ap2

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
                    try:
                        conn.request("POST", "/stop", headers={"X-Apple-Session-ID": "1"})
                        conn.getresponse()
                    finally:
                        conn.close()
                except Exception:
                    pass
        self.active_cast = None
