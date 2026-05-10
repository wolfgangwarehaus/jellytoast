"""
Chromecast + AirPlay v1 cast manager.
"""

import socket
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field

from modules.async_io import run_async

# Lazy-import the cast / mDNS deps. pychromecast pulls protobuf +
# zeroconf transitively at import (~80-200ms cold) and we only need it
# when the user actually opens the cast dialog. The flags are computed
# on first access via `_ensure_*` so callers can still gate behavior.
pychromecast = None  # type: ignore[assignment]
Zeroconf = None      # type: ignore[assignment]
ServiceBrowser = None  # type: ignore[assignment]
CHROMECAST_AVAILABLE: Optional[bool] = None
ZEROCONF_AVAILABLE: Optional[bool] = None


def _ensure_chromecast() -> bool:
    global pychromecast, CHROMECAST_AVAILABLE
    if CHROMECAST_AVAILABLE is None:
        try:
            import pychromecast as _pc
            pychromecast = _pc
            CHROMECAST_AVAILABLE = True
        except ImportError:
            CHROMECAST_AVAILABLE = False
    return bool(CHROMECAST_AVAILABLE)


def _ensure_zeroconf() -> bool:
    global Zeroconf, ServiceBrowser, ZEROCONF_AVAILABLE
    if ZEROCONF_AVAILABLE is None:
        try:
            from zeroconf import Zeroconf as _Zc, ServiceBrowser as _Sb
            Zeroconf = _Zc
            ServiceBrowser = _Sb
            ZEROCONF_AVAILABLE = True
        except ImportError:
            ZEROCONF_AVAILABLE = False
    return bool(ZEROCONF_AVAILABLE)


@dataclass
class CastDevice:
    name: str
    host: str
    port: int
    device_type: str  # "chromecast" | "airplay"
    uuid: str = ""
    cast_object: object = field(default=None, repr=False)


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
                name=display, host=host, port=info.port,
                device_type="airplay", uuid=name,
            )
            self.callback(list(self.devices.values()))

    def remove_service(self, zc, type_, name):
        self.devices.pop(name, None)
        self.callback(list(self.devices.values()))

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)


class CastManager:
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

    # ── Chromecast ──────────────────────────────────────────────────────────

    def discover_chromecasts(self):
        if not _ensure_chromecast():
            return
        # `pychromecast.get_chromecasts` is a blocking SSDP sweep;
        # offload to the shared thread pool per the project's async_io
        # convention so the GUI thread doesn't stall while the user's
        # network is being probed. Two recent latency wins:
        #   - timeout 5s → 3s. Real-world Chromecasts respond well
        #     under a second; 3s is plenty of slack for marginal
        #     networks without making the dialog feel sluggish.
        #   - Skip per-device ``cc.wait()`` here. That call blocks
        #     until the device's socket negotiation completes (often
        #     100-500ms each). We only need a usable Chromecast object
        #     when the user actually picks one to cast to —
        #     ``connect_to_chromecast`` already calls ``cc.wait()``
        #     then, so the discovery path can skip it entirely.
        def _go() -> List[CastDevice]:
            casts, _ = pychromecast.get_chromecasts(timeout=3)
            out: List[CastDevice] = []
            for cc in casts:
                out.append(CastDevice(
                    name=cc.name, host=cc.socket_client.host,
                    port=cc.socket_client.port, device_type="chromecast",
                    uuid=str(cc.uuid), cast_object=cc,
                ))
            return out

        def _on_result(devices: List[CastDevice]) -> None:
            self.chromecast_devices = devices
            self._notify()

        run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: print(f"Chromecast discovery: {e}"),
        )

    def connect_to_chromecast(self, dev: CastDevice) -> bool:
        """Establish a session with the device without sending any media.
        Used to pre-arm a Chromecast as the playback target before the
        user picks a track. Subsequent calls to cast_to_chromecast (via
        the router on MpvController) will route media here."""
        try:
            cc = dev.cast_object
            if cc is None:
                return False
            cc.wait()
            self.active_cast = dev
            return True
        except Exception as e:
            print(f"Chromecast connect: {e}")
            return False

    # Container → MIME map for Chromecast direct play. Anything not
    # in this dict gets transcoded to MP3 by the caller (MpvController)
    # before we get here. Source: Google Cast supported media formats.
    _CHROMECAST_AUDIO_MIME = {
        "mp3":  "audio/mpeg",
        "flac": "audio/flac",
        "ogg":  "audio/ogg",
        "oga":  "audio/ogg",
        "opus": "audio/ogg",  # Opus is shipped in OGG container
        "wav":  "audio/wav",
        "wave": "audio/wav",
        "m4a":  "audio/mp4",  # Assumes AAC; ALAC will fail and we'd transcode
        "mp4":  "audio/mp4",
        "aac":  "audio/aac",
        "webm": "audio/webm",
    }

    @classmethod
    def chromecast_audio_mime_for(cls, container: str) -> Optional[str]:
        """Return the Chromecast-direct-play MIME for `container`, or
        None if the format requires transcoding. Caller passes the
        Jellyfin item's `Container` field (e.g. 'flac', 'mp3', 'm4a')."""
        return cls._CHROMECAST_AUDIO_MIME.get((container or "").lower())

    def cast_to_chromecast(self, dev: CastDevice, url: str, title: str = "",
                            thumb: str = "", is_audio: bool = False,
                            content_type: Optional[str] = None,
                            current_time: float = 0.0) -> bool:
        try:
            cc = dev.cast_object
            if cc is None:
                return False
            cc.wait()
            mc = cc.media_controller
            # Caller can override the MIME (e.g. 'audio/flac' for direct
            # FLAC play). Fall back to the historical defaults if not
            # provided so existing call sites keep working.
            if content_type is None:
                content_type = "audio/mpeg" if is_audio else "video/mp4"
            stream_type = "BUFFERED"
            kwargs = dict(title=title, thumb=thumb, stream_type=stream_type,
                          autoplay=True)
            # Resume position. Default Media Receiver honors current_time
            # on play_media; passing 0 starts at the beginning.
            if current_time and current_time > 0.5:
                kwargs["current_time"] = current_time
            mc.play_media(url, content_type, **kwargs)
            mc.block_until_active(timeout=10)
            self.active_cast = dev
            return True
        except Exception as e:
            print(f"Chromecast play: {e}")
            return False

    def chromecast_pause(self):
        if self.active_cast and self.active_cast.device_type == "chromecast":
            cc = self.active_cast.cast_object
            if cc:
                mc = cc.media_controller
                if mc.status.player_is_playing:
                    mc.pause()
                else:
                    mc.play()

    def chromecast_seek(self, sec: float):
        if self.active_cast and self.active_cast.device_type == "chromecast":
            cc = self.active_cast.cast_object
            if cc:
                cc.media_controller.seek(sec)

    def chromecast_set_volume(self, percent: int):
        """Set Chromecast device volume (0-100)."""
        if self.active_cast and self.active_cast.device_type == "chromecast":
            cc = self.active_cast.cast_object
            if cc:
                try:
                    cc.set_volume(max(0.0, min(1.0, percent / 100.0)))
                except Exception as e:
                    print(f"Chromecast volume: {e}")

    def chromecast_stop(self):
        if self.active_cast and self.active_cast.device_type == "chromecast":
            cc = self.active_cast.cast_object
            if cc:
                try:
                    cc.media_controller.stop()
                    cc.quit_app()
                except Exception:
                    pass
        self.active_cast = None

    # ── AirPlay v1 ──────────────────────────────────────────────────────────

    def discover_airplay(self):
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
            print(f"AirPlay 2 discovery prep failed: {e}")
        if not _ensure_zeroconf():
            return
        def _go():
            zc = Zeroconf()
            listener = _AirPlayListener(
                lambda d: setattr(self, "airplay_devices", d) or self._notify()
            )
            browser = ServiceBrowser(zc, "_airplay._tcp.local.", listener)
            return zc, browser

        def _on_result(pair) -> None:
            self._zc, self._browser = pair

        run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: print(f"AirPlay discovery: {e}"),
        )

    def _discover_airplay_pyatv(self):
        """Streaming AirPlay scan via pyatv. Runs on the shared pool —
        pyatv.scan blocks for its timeout. The result list is
        translated into ``CastDevice`` entries with the pyatv config
        stuffed into ``cast_object`` so ``cast_to_airplay`` can route
        the cast through pyatv's AirPlay 2 client."""
        from modules import airplay2 as _ap2

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

        run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: print(f"AirPlay 2 discovery: {e}"),
        )

    def cast_to_airplay(self, dev: CastDevice, url: str, title: str = "") -> bool:
        # Route through pyatv (AirPlay 2) when the device was discovered
        # via pyatv — that path handles paired credentials, encrypted
        # control, and RTSP streaming. ``cast_object`` is the
        # ``AirPlay2Device`` returned by ``modules.airplay2.scan_sync``.
        from modules import airplay2 as _ap2
        if isinstance(dev.cast_object, _ap2.AirPlay2Device):
            return self._cast_to_airplay2(dev, url)
        # Legacy AirPlay 1 path — simple HTTP POST. Still useful for
        # ALAC speakers / older Apple TVs that pre-date AirPlay 2.
        try:
            import http.client
            body = f"Content-Location: {url}\nStart-Position: 0\n"
            conn = http.client.HTTPConnection(dev.host, dev.port, timeout=5)
            conn.request("POST", "/play", body=body.encode(), headers={
                "Content-Type": "text/parameters",
                "X-Apple-Session-ID": "1",
                "User-Agent": "MediaControl/1.0",
            })
            resp = conn.getresponse()
            conn.close()
            if resp.status in (200, 201):
                self.active_cast = dev
                return True
            return False
        except Exception as e:
            print(f"AirPlay cast: {e}")
            return False

    def _cast_to_airplay2(self, dev: CastDevice, url: str) -> bool:
        """Hand off to pyatv on a worker thread. ``cast_to_airplay``'s
        sync contract is preserved by blocking on the worker's
        completion via a one-shot QEventLoop. That keeps the existing
        callers (which expect a True/False return) happy without
        moving the cast button into an async story."""
        from modules import airplay2 as _ap2
        from PySide6.QtCore import QEventLoop
        ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]

        result = {"ok": False, "err": None}
        loop = QEventLoop()

        def _go() -> bool:
            _ap2.play_url_sync(ap2_dev, url)
            return True

        def _on_result(_):
            result["ok"] = True
            loop.quit()

        def _on_error(e):
            result["err"] = e
            loop.quit()

        run_async(_go, on_result=_on_result, on_error=_on_error)
        loop.exec()  # blocks until worker completes
        if result["ok"]:
            self.active_cast = dev
            return True
        err = result["err"]
        if isinstance(err, _ap2.PairingRequired):
            # Tag a flag the cast dialog can pick up to launch the
            # pairing UI. For now we just print and return False —
            # the pairing dialog ships in a follow-up.
            print(f"AirPlay 2 cast: pairing required for {dev.name}")
        else:
            print(f"AirPlay 2 cast: {err}")
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
                        self.active_cast.host, self.active_cast.port, timeout=3)
                    conn.request("POST", "/stop", headers={"X-Apple-Session-ID": "1"})
                    conn.getresponse()
                    conn.close()
                except Exception:
                    pass
        self.active_cast = None

    # ── Common ──────────────────────────────────────────────────────────────

    def discover_all(self):
        self.discover_chromecasts()
        self.discover_airplay()

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
