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

from ._common import CastDevice, CastType, _type_enabled

logger = logging.getLogger(__name__)


class _ChromecastMixin:
    # ── Chromecast ──────────────────────────────────────────────────────────

    def discover_chromecasts(self):
        from modules import cast_manager as _pkg

        if not _type_enabled("chromecast"):
            return
        if not _pkg._ensure_chromecast():
            return

        # Discovery moved from the legacy blocking ``get_chromecasts``
        # one-shot sweep (deprecated since pychromecast 13 — the library
        # has signalled removal multiple times) to ``CastBrowser`` +
        # ``SimpleCastListener``: start discovery, let mDNS responses
        # buffer into the listener for the same ~3 s window the old
        # ``timeout=3`` allowed, snapshot the discovered set, stop.
        #
        # Two preserved behaviours from the prior implementation:
        #   - 3 s sweep window. Real-world Chromecasts respond well
        #     under a second; 3 s is plenty of slack for marginal
        #     networks without making the dialog feel sluggish. Tests
        #     patch ``DISCOVERY_WINDOW_S`` to 0.0 so the gating tests
        #     stay fast.
        #   - Skip per-device ``cc.wait()`` here.
        #     ``get_chromecast_from_host`` materialises a Chromecast
        #     handle without negotiating the socket (verified lazy — no
        #     eager connection); ``connect_to_chromecast`` /
        #     ``cast_to_chromecast`` run the ``cc.wait()`` then, when the
        #     user actually picks a device.
        #
        # Materialise by HOST, not by mDNS service. The service path
        # (``get_chromecast_from_cast_info``) makes the socket client
        # re-resolve the host through the zeroconf instance at CONNECT
        # time — but the discovery sweep's zeroconf loop is already
        # stopped by ``stop_discovery`` (every cast happens afterwards),
        # so those connects fail with "Zeroconf instance loop must be
        # running". Host-based connects straight to the host:port the
        # sweep just resolved, needs no live zeroconf, and also
        # materialises Google-TV/webOS receivers that raise
        # ``ZeroConfInstanceRequired`` on the service path.
        # See reference_chromecast_tailscale_discovery.
        def _go() -> List[CastDevice]:
            discovered_uuids: List[object] = []

            def _on_add(uuid, _service):
                discovered_uuids.append(uuid)

            listener = _pkg.SimpleCastListener(add_callback=_on_add)
            # Bind the sweep to the LAN interfaces, excluding any
            # Tailscale/CGNAT overlay — a default Zeroconf() binds across
            # all interfaces and, with Tailscale up, sends the
            # _googlecast._tcp query out the tunnel and finds nothing
            # (the "Chromecasts stopped showing up" bug). None falls back
            # to CastBrowser's own default-bound instance.
            zc = _pkg._make_discovery_zeroconf()
            browser = _pkg.CastBrowser(listener, zc)
            browser.start_discovery()
            try:
                time.sleep(_pkg.DISCOVERY_WINDOW_S)
                out: List[CastDevice] = []
                devices = browser.devices
                # Snapshot the uuid list — the zeroconf service thread
                # is still appending into ``discovered_uuids`` until
                # ``stop_discovery`` runs in the finally. Iterating the
                # live list would race with a late add_callback.
                for uuid in list(discovered_uuids):
                    info = devices.get(uuid)
                    if info is None:
                        continue
                    try:
                        # Host-based handle (see the block comment above):
                        # connects straight to the resolved host:port at
                        # ``cc.wait()`` time, with no dependency on a live
                        # zeroconf loop.
                        cc = _pkg.get_chromecast_from_host(
                            (
                                info.host,
                                info.port,
                                info.uuid,
                                info.model_name,
                                info.friendly_name,
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "Chromecast materialise %r failed: %s",
                            getattr(info, "friendly_name", uuid),
                            exc,
                        )
                        continue
                    out.append(
                        CastDevice(
                            name=info.friendly_name or "Chromecast",
                            host=info.host,
                            port=info.port,
                            device_type=CastType.CHROMECAST,
                            uuid=str(info.uuid),
                            cast_object=cc,
                            cast_type=info.cast_type or "cast",
                        )
                    )
                return out
            finally:
                try:
                    browser.stop_discovery()
                except Exception as exc:
                    logger.warning("Chromecast stop_discovery: %s", exc)
                # The sweep's zeroconf is only needed for discovery now
                # (host-based handles don't use it to connect), so close
                # it here. CastBrowser closes the instance it creates for
                # zconf=None itself, so only our own LAN-bound one needs
                # this.
                if zc is not None:
                    try:
                        zc.close()
                    except Exception as exc:
                        logger.warning("Chromecast discovery zeroconf close: %s", exc)

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
            # Bounded: a bare wait() blocks the worker forever if the
            # device is powered off / off-network. RequestTimeout is
            # caught below → reported as a failed connect.
            cc.wait(timeout=10)
            self._arm_active_cast(dev)
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
            # Bounded so a powered-off / off-network device fails the cast
            # instead of hanging the worker forever (block_until_active +
            # the play-state poll below have their own timeouts).
            cc.wait(timeout=10)
            mc = cc.media_controller
            # Route the stream (and cover art) through the local cast
            # proxy when the server isn't directly reachable by the
            # device — Tailscale / remote / self-signed hosts. Honors
            # the cast_stream_routing setting and degrades to the
            # original URL on any failure.
            #
            # Live/radio streams are SKIPPED: an internet-radio URL is a
            # public CDN the cast device fetches directly, so routing it
            # through the local proxy (a fixed port that usually needs a
            # firewall rule) only adds a failure point and can stall an
            # endless ICY stream. The proxy is for the user's own otherwise-
            # unreachable media server, not already-public radio.
            if not is_live:
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
            # GROUP: put each saved member speaker at its saved level BEFORE
            # the media starts, so audio begins at those levels rather than
            # the speakers' current (possibly loud) volume and then snapping
            # down. Also snapshots the pre-cast levels for the on-stop restore.
            if getattr(dev, "cast_type", "") == "group":
                self.prepare_group_volume_before_media(dev)
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
                    self._arm_active_cast(dev)
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
                self._arm_active_cast(dev)
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
        if self.active_cast and self.active_cast.device_type == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc:
                mc = cc.media_controller
                if mc.status.player_is_playing:
                    mc.pause()
                else:
                    mc.play()

    def chromecast_seek(self, sec: float):
        if self.active_cast and self.active_cast.device_type == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc:
                cc.media_controller.seek(sec)

    def chromecast_set_volume(self, percent: int):
        """Set Chromecast device volume (0-100)."""
        if self.active_cast and self.active_cast.device_type == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc:
                try:
                    cc.set_volume(max(0.0, min(1.0, percent / 100.0)))
                except Exception as e:
                    logger.warning("Chromecast volume: %s", e)

    def chromecast_get_volume(self) -> Optional[int]:
        """The device's current volume as 0-100, or None if unreadable.
        Reads the locally-cached receiver status (``volume_level`` is
        0.0-1.0), so there's no network round-trip. Used to snapshot the
        pre-cast level before we override it, so it can be restored on
        disconnect."""
        if self.active_cast and self.active_cast.device_type == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc:
                try:
                    lvl = getattr(cc.status, "volume_level", None)
                    if lvl is not None:
                        return int(round(float(lvl) * 100))
                except Exception as e:
                    logger.warning("Chromecast volume read: %s", e)
        return None

    def chromecast_stop(self):
        if self.active_cast and self.active_cast.device_type == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc:
                try:
                    cc.media_controller.stop()
                    cc.quit_app()
                except Exception:
                    pass
        self.active_cast = None

    # ── Chromecast groups (per-member volume) ───────────────────────────────

    def prepare_group_volume_before_media(self, group_dev: CastDevice) -> None:
        """Set each SAVED member speaker to its saved level BEFORE play_media,
        snapshotting its current level first so stop_cast can hand it back.

        This runs synchronously inside the (already off-GUI-thread) cast path,
        before the media starts, so audio begins AT the saved levels instead
        of the speakers' current (possibly loud) volume and then snapping
        down. Keyed off the saved balance's own uuids (no slow multizone
        discovery), and a no-op when there's no saved balance for this group.
        Runs once per cast session — guarded by ``_pre_cast_member_volumes``
        so an auto-advance / re-cast doesn't re-snapshot the already-applied
        levels as if they were the pre-cast ones."""
        if self._pre_cast_member_volumes is not None:
            return
        from modules.settings import get_settings

        group_uuid = getattr(group_dev, "uuid", "")
        saved = get_settings().cast_member_volumes.get(group_uuid, {}) if group_uuid else {}
        if not saved:
            return
        snap: List[Dict] = []
        for uuid, level in saved.items():
            dev = next((d for d in self.chromecast_devices if d.uuid == uuid), None)
            if dev is None or dev.cast_object is None:
                continue
            try:
                cc = dev.cast_object
                cc.wait(timeout=3)
                cur = getattr(cc.status, "volume_level", None)
                if cur is None:
                    continue
                cur_pct = int(round(float(cur) * 100))
                snap.append({"uuid": uuid, "volume": cur_pct})
                if cur_pct != int(level):
                    cc.set_volume(max(0.0, min(1.0, int(level) / 100.0)))
            except Exception as e:
                logger.warning("group volume pre-set for %s: %s", uuid, e)
        self._pre_cast_member_volumes = snap

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
