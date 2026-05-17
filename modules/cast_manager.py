"""
Chromecast + AirPlay v1 cast manager.
"""

import socket
import time
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field

from modules.async_io import run_async

# Lazy-import the cast / mDNS deps. pychromecast pulls protobuf +
# zeroconf transitively at import (~80-200ms cold) and we only need it
# when the user actually opens the cast dialog. The flags are computed
# on first access via `_ensure_*` so callers can still gate behavior.
pychromecast = None  # type: ignore[assignment]
Zeroconf = None  # type: ignore[assignment]
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
    # pychromecast cast_type: "cast" (video), "audio", or "group" (a
    # multi-room speaker group). "group" unlocks per-member volume in
    # the volume popup. AirPlay devices leave this at "".
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
        if not self._type_enabled("chromecast"):
            return
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
                out.append(
                    CastDevice(
                        name=cc.name,
                        host=cc.socket_client.host,
                        port=cc.socket_client.port,
                        device_type="chromecast",
                        uuid=str(cc.uuid),
                        cast_object=cc,
                        cast_type=getattr(cc, "cast_type", "cast"),
                    )
                )
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
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "oga": "audio/ogg",
        "opus": "audio/ogg",  # Opus is shipped in OGG container
        "wav": "audio/wav",
        "wave": "audio/wav",
        "m4a": "audio/mp4",  # Assumes AAC; ALAC will fail and we'd transcode
        "mp4": "audio/mp4",
        "aac": "audio/aac",
        "webm": "audio/webm",
    }

    @classmethod
    def chromecast_audio_mime_for(cls, container: str) -> Optional[str]:
        """Return the Chromecast-direct-play MIME for `container`, or
        None if the format requires transcoding. Caller passes the
        Jellyfin item's `Container` field (e.g. 'flac', 'mp3', 'm4a')."""
        return cls._CHROMECAST_AUDIO_MIME.get((container or "").lower())

    @staticmethod
    def _dump_cast_status(mc) -> None:
        """Print everything the receiver told us about the failed media
        session — the fastest way to tell a network-reachability reject
        apart from a codec/container reject apart from a never-loaded
        session. Best-effort: any attribute may be missing on older
        pychromecast."""
        st = mc.status
        fields = (
            "player_state",
            "idle_reason",
            "content_id",
            "content_type",
            "stream_type",
            "duration",
            "current_time",
            "media_custom_data",
            "supported_media_commands",
            "media_metadata",
        )
        bits = []
        for f in fields:
            try:
                bits.append(f"{f}={getattr(st, f, None)!r}")
            except Exception:
                pass
        print("[cast-dbg] media status: " + " ".join(bits), flush=True)

    def cast_to_chromecast(
        self,
        dev: CastDevice,
        url: str,
        title: str = "",
        thumb: str = "",
        is_audio: bool = False,
        content_type: Optional[str] = None,
        current_time: float = 0.0,
    ) -> bool:
        try:
            cc = dev.cast_object
            if cc is None:
                return False
            cc.wait()
            mc = cc.media_controller
            # Route the stream (and cover art) through the local cast
            # proxy when the server isn't directly reachable by the
            # device — Tailscale / remote / self-signed hosts. Honors
            # the cast_stream_routing setting and degrades to the
            # original URL on any failure.
            from modules.cast_proxy import resolve_cast_url

            url = resolve_cast_url(url)
            if thumb:
                thumb = resolve_cast_url(thumb)
            # Caller can override the MIME (e.g. 'audio/flac' for direct
            # FLAC play). Fall back to the historical defaults if not
            # provided so existing call sites keep working.
            if content_type is None:
                content_type = "audio/mpeg" if is_audio else "video/mp4"
            stream_type = "BUFFERED"
            kwargs = dict(title=title, thumb=thumb, stream_type=stream_type, autoplay=True)
            # Resume position. Default Media Receiver honors current_time
            # on play_media; passing 0 starts at the beginning.
            if current_time and current_time > 0.5:
                kwargs["current_time"] = current_time
            print(
                f"[cast-dbg] play_media: app={cc.app_id!r} "
                f"content_type={content_type!r} current_time={current_time} "
                f"url={url}",
                flush=True,
            )
            mc.play_media(url, content_type, **kwargs)
            # block_until_active only waits for the media *session* to be
            # established on the receiver — it returns even when the
            # receiver then rejects the URL itself (host unreachable from
            # the device's network, a stale Subsonic salt/token, an
            # unsupported codec). So poll the real player_state afterwards:
            # only PLAYING / BUFFERING means the cast actually took. An
            # IDLE/ERROR (or never leaving the gate) is a failure we must
            # report, otherwise the UI claims "playing" on a silent device.
            mc.block_until_active(timeout=8)
            print(
                f"[cast-dbg] after block_until_active: "
                f"state={getattr(mc.status, 'player_state', None)!r} "
                f"idle_reason={getattr(mc.status, 'idle_reason', None)!r}",
                flush=True,
            )
            deadline = time.monotonic() + 12.0
            last_seen = None
            while time.monotonic() < deadline:
                st = mc.status
                state = getattr(st, "player_state", None)
                idle_reason = getattr(st, "idle_reason", None)
                if (state, idle_reason) != last_seen:
                    print(
                        f"[cast-dbg] poll: state={state!r} idle_reason={idle_reason!r}", flush=True
                    )
                    last_seen = (state, idle_reason)
                if state in ("PLAYING", "BUFFERING"):
                    self.active_cast = dev
                    return True
                if state == "IDLE" and idle_reason == "ERROR":
                    print(
                        "Chromecast play: receiver rejected media "
                        "(idle/ERROR) — the device could not load the "
                        "stream URL. Most likely it's unreachable from "
                        "the speaker's network (self-signed cert, "
                        "LAN-only hostname) or the codec is unsupported.",
                        flush=True,
                    )
                    self._dump_cast_status(mc)
                    return False
                time.sleep(0.25)
            final = getattr(mc.status, "player_state", None)
            if final in ("PLAYING", "BUFFERING", "PAUSED"):
                self.active_cast = dev
                return True
            print(
                f"Chromecast play: receiver never started (state={final}) "
                f"— the speaker accepted the cast session but never began "
                f"playback within the timeout.",
                flush=True,
            )
            self._dump_cast_status(mc)
            return False
        except Exception as e:
            print(f"Chromecast play: {e}")
            return False

    def connect_to_chromecast_async(
        self, dev: CastDevice, on_done: Optional[Callable[[bool], None]] = None
    ):
        """Non-blocking ``connect_to_chromecast``. The sync version blocks
        on ``cc.wait()`` — socket negotiation that is usually a few
        hundred ms but can stretch to seconds on a marginal network —
        which freezes the cast dialog if run on the GUI thread. Offload
        to the shared pool; ``on_done(ok)`` fires back on the GUI thread."""

        def _go() -> bool:
            return self.connect_to_chromecast(dev)

        def _ok(ok: bool) -> None:
            if on_done:
                on_done(bool(ok))

        def _err(e: Exception) -> None:
            print(f"Chromecast connect: {e}")
            if on_done:
                on_done(False)

        run_async(_go, on_result=_ok, on_error=_err)

    def cast_to_chromecast_async(
        self,
        dev: CastDevice,
        url: str,
        title: str = "",
        thumb: str = "",
        is_audio: bool = False,
        content_type: Optional[str] = None,
        current_time: float = 0.0,
        on_done: Optional[Callable[[bool], None]] = None,
    ):
        """Non-blocking ``cast_to_chromecast``. The sync version blocks the
        caller for as long as ``cc.wait()`` + ``block_until_active`` + the
        play-state poll take (up to ~16s if the receiver is slow or the
        URL is unreachable) — fine on a worker thread, a hard UI freeze on
        the GUI thread. ``on_done(ok)`` fires on the GUI thread once the
        receiver has actually started playing (or definitively failed)."""

        def _go() -> bool:
            return self.cast_to_chromecast(
                dev,
                url,
                title=title,
                thumb=thumb,
                is_audio=is_audio,
                content_type=content_type,
                current_time=current_time,
            )

        def _ok(ok: bool) -> None:
            if on_done:
                on_done(bool(ok))

        def _err(e: Exception) -> None:
            print(f"Chromecast play: {e}")
            if on_done:
                on_done(False)

        run_async(_go, on_result=_ok, on_error=_err)

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

    # ── Chromecast groups (per-member volume) ───────────────────────────────

    def group_members_async(self, group_dev: CastDevice, on_result: Callable):
        """Resolve a Chromecast group's member speakers + their current
        volumes, off the GUI thread. ``on_result(list)`` fires on the
        GUI thread with ``[{uuid, name, volume, available}]`` —
        ``volume`` is 0-100, ``available`` is False when the member
        wasn't in the discovery cache (can't be read or controlled).

        pychromecast's MultizoneController only enumerates members
        (uuid + name) — it has no per-member volume. So each member's
        physical Chromecast is connected to directly; its device-level
        volume is independent of the group session."""

        def _go() -> List[Dict]:
            from pychromecast.controllers.multizone import MultizoneController

            group_cc = group_dev.cast_object
            if group_cc is None:
                return []
            group_cc.wait(timeout=6)
            mz = MultizoneController(group_cc.uuid)
            group_cc.register_handler(mz)
            # update_members() sends a GET_STATUS; the group's
            # TYPE_MULTIZONE_STATUS reply lands asynchronously a beat
            # later. mz.members is a list of member uuids (not a dict)
            # — poll it until it fills, then give a short grace for any
            # stragglers so we don't read a half-populated batch.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and not mz.members:
                try:
                    mz.update_members()
                except Exception:
                    pass
                time.sleep(0.3)
            if mz.members:
                time.sleep(0.5)
            # The {uuid: name} mapping lives in the private _members;
            # the public .members property only exposes the uuid list.
            name_by_uuid = dict(getattr(mz, "_members", {}) or {})
            member_uuids = list(mz.members)
            print(
                f"[cast] group {group_dev.name!r}: {len(member_uuids)} "
                f"member(s) — "
                f"{[name_by_uuid.get(u, u) for u in member_uuids]}",
                flush=True,
            )
            out: List[Dict] = []
            for uuid in member_uuids:
                dev = next((d for d in self.chromecast_devices if d.uuid == uuid), None)
                # Prefer the group-reported name; fall back to the
                # discovery-cache name, then the uuid.
                name = name_by_uuid.get(uuid) or (dev.name if dev is not None else "") or "Speaker"
                vol, available = 50, False
                if dev is not None and dev.cast_object is not None:
                    try:
                        member_cc = dev.cast_object
                        member_cc.wait(timeout=5)
                        lvl = getattr(member_cc.status, "volume_level", None)
                        if lvl is not None:
                            vol = int(round(lvl * 100))
                        available = True
                    except Exception as e:
                        print(f"[cast] member volume read failed for {name!r}: {e}", flush=True)
                out.append({"uuid": uuid, "name": name, "volume": vol, "available": available})
            return out

        run_async(
            _go,
            on_result=on_result,
            on_error=lambda e: (print(f"[cast] group_members: {e}", flush=True), on_result([])),
        )

    def set_member_volume_async(
        self, member_uuid: str, level_pct: int, on_done: Optional[Callable] = None
    ):
        """Set one group-member speaker's volume (0-100) off the GUI
        thread. Connects to the member's physical Chromecast directly —
        its device volume is independent of the group session, so this
        works mid-playback."""

        def _go() -> bool:
            dev = next((d for d in self.chromecast_devices if d.uuid == member_uuid), None)
            if dev is None or dev.cast_object is None:
                return False
            cc = dev.cast_object
            cc.wait()
            cc.set_volume(max(0.0, min(1.0, level_pct / 100.0)))
            return True

        run_async(
            _go,
            on_result=lambda ok: on_done(bool(ok)) if on_done else None,
            on_error=lambda e: (
                print(f"[cast] set_member_volume: {e}", flush=True),
                on_done(False) if on_done else None,
            ),
        )

    # ── AirPlay v1 ──────────────────────────────────────────────────────────

    def discover_airplay(self):
        if not self._type_enabled("airplay"):
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

        # Same proxy routing as the Chromecast path — an AirPlay
        # receiver can't reach a Tailscale / remote server URL either.
        from modules.cast_proxy import resolve_cast_url

        url = resolve_cast_url(url)
        print(
            f"[ap2-dbg] cast_to_airplay: dev={dev.name!r} "
            f"cast_object_type={type(dev.cast_object).__name__}",
            flush=True,
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
        print(
            f"[ap2-dbg] _cast_to_airplay2: dev={ap2_dev.name!r} "
            f"id={ap2_dev.identifier!r} url_len={len(url)}",
            flush=True,
        )

        result = {"ok": False, "err": None}
        loop = QEventLoop()

        def _go() -> bool:
            print("[ap2-dbg] worker: calling play_url_sync", flush=True)
            _ap2.play_url_sync(ap2_dev, url)
            print("[ap2-dbg] worker: play_url_sync returned", flush=True)
            return True

        def _on_result(_):
            result["ok"] = True
            loop.quit()

        def _on_error(e):
            print(f"[ap2-dbg] worker on_error: {type(e).__name__}: {e}", flush=True)
            result["err"] = e
            loop.quit()

        run_async(_go, on_result=_on_result, on_error=_on_error)
        loop.exec()  # blocks until worker completes
        if result["ok"]:
            print(f"[ap2-dbg] _cast_to_airplay2: success for {dev.name!r}", flush=True)
            self.active_cast = dev
            return True
        err = result["err"]
        if isinstance(err, _ap2.PairingRequired):
            # Tag a flag the cast dialog can pick up to launch the
            # pairing UI. For now we just print and return False —
            # the pairing dialog ships in a follow-up.
            print(f"AirPlay 2 cast: pairing required for {dev.name}", flush=True)
        else:
            print(f"AirPlay 2 cast: {type(err).__name__}: {err}", flush=True)
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

    # ── Common ──────────────────────────────────────────────────────────────

    def _type_enabled(self, kind: str) -> bool:
        """Per-protocol gate: a user can disable a cast type they don't
        own so discovery skips it entirely. Reads the QSettings directly
        rather than importing modules.settings so this module stays
        light at import time (settings.py runs the legacy-org migration
        on first construction)."""
        from PySide6.QtCore import QSettings

        qs = QSettings("jellytoast", "jellytoast")
        return bool(qs.value(f"cast/{kind}_enabled", True, type=bool))

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
