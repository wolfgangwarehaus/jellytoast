"""
Chromecast + AirPlay v1 cast manager.
"""

import threading
import socket
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field

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
        if not _ensure_zeroconf():
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
