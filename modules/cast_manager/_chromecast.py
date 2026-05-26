"""Chromecast discovery, connection, transport, and group-member
control. Mixed into ``CastManager`` via ``_ChromecastMixin``.

Monkeypatch indirection: the test suite patches ``pychromecast`` and
``run_async`` on the ``modules.cast_manager`` package namespace. This
module therefore resolves both symbols through the package
(``from modules import cast_manager as _pkg``) at call time rather
than binding them at import — a direct ``from ._common import
run_async`` would freeze a stale reference and ignore the patch.
"""

import logging
import time
from typing import Callable, Dict, List, Optional

from ._common import CastDevice, _type_enabled

logger = logging.getLogger(__name__)


class _ChromecastMixin:
    # ── Chromecast ──────────────────────────────────────────────────────────

    def discover_chromecasts(self):
        from modules import cast_manager as _pkg

        if not _type_enabled("chromecast"):
            return
        if not _pkg._ensure_chromecast():
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
            casts, _ = _pkg.pychromecast.get_chromecasts(timeout=3)
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

        _pkg.run_async(
            _go,
            on_result=_on_result,
            on_error=lambda e: logger.warning("Chromecast discovery: %s", e),
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
            logger.warning("Chromecast connect: %s", e)
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
        logger.debug("media status: %s", " ".join(bits))

    def cast_to_chromecast(
        self,
        dev: CastDevice,
        url: str,
        title: str = "",
        thumb: str = "",
        is_audio: bool = False,
        content_type: Optional[str] = None,
        current_time: float = 0.0,
        is_live: bool = False,
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
            # LIVE for endless streams (internet radio); BUFFERED for VOD
            # tracks with a known duration. Default Media Receiver rejects
            # an unsized BUFFERED Icecast stream into IDLE/ERROR.
            stream_type = "LIVE" if is_live else "BUFFERED"
            kwargs = dict(title=title, thumb=thumb, stream_type=stream_type, autoplay=True)
            # Resume position. Default Media Receiver honors current_time
            # on play_media; passing 0 starts at the beginning.
            if current_time and current_time > 0.5:
                kwargs["current_time"] = current_time
            logger.debug(
                "play_media: app=%r content_type=%r current_time=%s url=%s",
                cc.app_id,
                content_type,
                current_time,
                url,
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
            logger.debug(
                "after block_until_active: state=%r idle_reason=%r",
                getattr(mc.status, "player_state", None),
                getattr(mc.status, "idle_reason", None),
            )
            deadline = time.monotonic() + 12.0
            last_seen = None
            while time.monotonic() < deadline:
                st = mc.status
                state = getattr(st, "player_state", None)
                idle_reason = getattr(st, "idle_reason", None)
                if (state, idle_reason) != last_seen:
                    logger.debug(
                        "poll: state=%r idle_reason=%r", state, idle_reason
                    )
                    last_seen = (state, idle_reason)
                if state in ("PLAYING", "BUFFERING"):
                    self.active_cast = dev
                    return True
                if state == "IDLE" and idle_reason == "ERROR":
                    logger.warning(
                        "Chromecast play: receiver rejected media "
                        "(idle/ERROR) — the device could not load the "
                        "stream URL. Most likely it's unreachable from "
                        "the speaker's network (self-signed cert, "
                        "LAN-only hostname) or the codec is unsupported."
                    )
                    self._dump_cast_status(mc)
                    return False
                time.sleep(0.25)
            final = getattr(mc.status, "player_state", None)
            if final in ("PLAYING", "BUFFERING", "PAUSED"):
                self.active_cast = dev
                return True
            logger.warning(
                "Chromecast play: receiver never started (state=%s) "
                "— the speaker accepted the cast session but never began "
                "playback within the timeout.",
                final,
            )
            self._dump_cast_status(mc)
            return False
        except Exception as e:
            logger.warning("Chromecast play: %s", e)
            return False

    def connect_to_chromecast_async(
        self, dev: CastDevice, on_done: Optional[Callable[[bool], None]] = None
    ):
        """Non-blocking ``connect_to_chromecast``. The sync version blocks
        on ``cc.wait()`` — socket negotiation that is usually a few
        hundred ms but can stretch to seconds on a marginal network —
        which freezes the cast dialog if run on the GUI thread. Offload
        to the shared pool; ``on_done(ok)`` fires back on the GUI thread."""
        from modules import cast_manager as _pkg

        def _go() -> bool:
            return self.connect_to_chromecast(dev)

        def _ok(ok: bool) -> None:
            if on_done:
                on_done(bool(ok))

        def _err(e: Exception) -> None:
            logger.warning("Chromecast connect: %s", e)
            if on_done:
                on_done(False)

        _pkg.run_async(_go, on_result=_ok, on_error=_err)

    def cast_to_chromecast_async(
        self,
        dev: CastDevice,
        url: str,
        title: str = "",
        thumb: str = "",
        is_audio: bool = False,
        content_type: Optional[str] = None,
        current_time: float = 0.0,
        is_live: bool = False,
        on_done: Optional[Callable[[bool], None]] = None,
    ):
        """Non-blocking ``cast_to_chromecast``. The sync version blocks the
        caller for as long as ``cc.wait()`` + ``block_until_active`` + the
        play-state poll take (up to ~16s if the receiver is slow or the
        URL is unreachable) — fine on a worker thread, a hard UI freeze on
        the GUI thread. ``on_done(ok)`` fires on the GUI thread once the
        receiver has actually started playing (or definitively failed)."""
        from modules import cast_manager as _pkg

        def _go() -> bool:
            return self.cast_to_chromecast(
                dev,
                url,
                title=title,
                thumb=thumb,
                is_audio=is_audio,
                content_type=content_type,
                current_time=current_time,
                is_live=is_live,
            )

        def _ok(ok: bool) -> None:
            if on_done:
                on_done(bool(ok))

        def _err(e: Exception) -> None:
            logger.warning("Chromecast play: %s", e)
            if on_done:
                on_done(False)

        _pkg.run_async(_go, on_result=_ok, on_error=_err)

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
                    logger.warning("Chromecast volume: %s", e)

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
        from modules import cast_manager as _pkg

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
            logger.debug(
                "group %r: %s member(s) — %s",
                group_dev.name,
                len(member_uuids),
                [name_by_uuid.get(u, u) for u in member_uuids],
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
                        logger.warning(
                            "member volume read failed for %r: %s", name, e
                        )
                out.append({"uuid": uuid, "name": name, "volume": vol, "available": available})
            return out

        _pkg.run_async(
            _go,
            on_result=on_result,
            on_error=lambda e: (logger.warning("group_members: %s", e), on_result([])),
        )

    def set_member_volume_async(
        self, member_uuid: str, level_pct: int, on_done: Optional[Callable] = None
    ):
        """Set one group-member speaker's volume (0-100) off the GUI
        thread. Connects to the member's physical Chromecast directly —
        its device volume is independent of the group session, so this
        works mid-playback."""
        from modules import cast_manager as _pkg

        def _go() -> bool:
            dev = next((d for d in self.chromecast_devices if d.uuid == member_uuid), None)
            if dev is None or dev.cast_object is None:
                return False
            cc = dev.cast_object
            cc.wait()
            cc.set_volume(max(0.0, min(1.0, level_pct / 100.0)))
            return True

        _pkg.run_async(
            _go,
            on_result=lambda ok: on_done(bool(ok)) if on_done else None,
            on_error=lambda e: (
                logger.warning("set_member_volume: %s", e),
                on_done(False) if on_done else None,
            ),
        )
