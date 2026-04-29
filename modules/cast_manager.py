"""
Chromecast + AirPlay v1 cast manager.
"""

import threading
import socket
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field

try:
    import pychromecast
    CHROMECAST_AVAILABLE = True
except ImportError:
    CHROMECAST_AVAILABLE = False

try:
    from zeroconf import Zeroconf, ServiceBrowser
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False


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
        if not CHROMECAST_AVAILABLE:
            return
        def _go():
            try:
                casts, _ = pychromecast.get_chromecasts(timeout=5)
                self.chromecast_devices = []
                for cc in casts:
                    cc.wait()
                    self.chromecast_devices.append(CastDevice(
                        name=cc.name, host=cc.socket_client.host,
                        port=cc.socket_client.port, device_type="chromecast",
                        uuid=str(cc.uuid), cast_object=cc,
                    ))
                self._notify()
            except Exception as e:
                print(f"Chromecast discovery: {e}")
        threading.Thread(target=_go, daemon=True).start()

    def cast_to_chromecast(self, dev: CastDevice, url: str, title: str = "",
                            thumb: str = "", is_audio: bool = False) -> bool:
        try:
            cc = dev.cast_object
            if cc is None:
                return False
            cc.wait()
            mc = cc.media_controller
            content_type = "audio/mpeg" if is_audio else "video/mp4"
            stream_type = "BUFFERED"
            mc.play_media(url, content_type, title=title, thumb=thumb,
                          stream_type=stream_type, autoplay=True)
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
        if not ZEROCONF_AVAILABLE:
            return
        def _go():
            try:
                self._zc = Zeroconf()
                listener = _AirPlayListener(lambda d: setattr(self, "airplay_devices", d) or self._notify())
                self._browser = ServiceBrowser(self._zc, "_airplay._tcp.local.", listener)
            except Exception as e:
                print(f"AirPlay discovery: {e}")
        threading.Thread(target=_go, daemon=True).start()

    def cast_to_airplay(self, dev: CastDevice, url: str, title: str = "") -> bool:
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

    def airplay_stop(self):
        if self.active_cast and self.active_cast.device_type == "airplay":
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
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
