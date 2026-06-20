"""``CastManager`` — the thin orchestrator. Composes ``_ChromecastMixin``
and ``_AirplayMixin``; owns the device caches, the active-cast slot,
the devices-changed callback, and the cross-protocol lifecycle
(discovery fanout, ``stop_cast``, ``cleanup``).
"""

import logging
from typing import Callable, List, Optional

from ._airplay import _AirplayMixin
from ._chromecast import _ChromecastMixin
from ._common import CastDevice, CastType
from ._others import _OtherProtocolsMixin

logger = logging.getLogger(__name__)

# When the device's pre-cast volume can't be read, restore to this uniform
# level on disconnect rather than leaving it at our cast level (a TV speaker
# used for music shouldn't be left wrong for TV). Design call: previous
# level when readable, this fallback otherwise.
_CAST_VOLUME_RESTORE_FALLBACK = 40


def _radio_mime_for(url: str) -> str:
    """Best-effort Content-Type for an internet-radio stream from its URL.

    The Chromecast Default Media Receiver rejects a session whose declared
    ``content_type`` doesn't match the bytes — so an AAC SomaFM stream sent as
    ``audio/mpeg`` lands in IDLE/ERROR (the reported "can't cast radio" bug,
    since the curated SomaFM presets are AAC). Map the common codec suffixes;
    fall back to ``audio/mpeg`` (broadest support + the historical default)
    when the URL gives no hint."""
    u = (url or "").lower().split("?", 1)[0]
    if u.endswith(("aac", ".m4a")):
        return "audio/aac"
    if u.endswith((".ogg", ".oga", ".opus")):
        return "audio/ogg"
    if u.endswith(".flac"):
        return "audio/flac"
    return "audio/mpeg"


class CastManager(_ChromecastMixin, _AirplayMixin, _OtherProtocolsMixin):
    def __init__(self):
        self.chromecast_devices: List[CastDevice] = []
        self.airplay_devices: List[CastDevice] = []
        self.dlna_devices: List[CastDevice] = []
        self.sonos_devices: List[CastDevice] = []
        self.snapcast_devices: List[CastDevice] = []
        self.active_cast: Optional[CastDevice] = None
        self._zc = None  # AirPlay v1 mDNS ServiceBrowser's zeroconf
        self._browser = None
        self._on_update: Optional[Callable] = None
        # The active device's volume captured at connect (before we force
        # the cast initial volume), restored on stop_cast. None = unread.
        self._pre_cast_device_volume: Optional[int] = None
        # Chromecast GROUPS get per-member handling instead: each member
        # speaker's pre-cast device volume, snapshotted at connect and
        # handed back on stop_cast. [{uuid, volume}]. None = not a group /
        # not yet read.
        self._pre_cast_member_volumes: Optional[List[dict]] = None

    def set_devices_callback(self, cb: Callable):
        self._on_update = cb

    def _notify(self):
        if self._on_update:
            self._on_update(self.get_all_devices())

    def _arm_active_cast(self, dev, *, reset_paused: bool = False):
        """Record ``dev`` as the active cast target — ON THE GUI THREAD.

        The Chromecast/DLNA/Sonos ``cast_to_*`` workers run on the async
        pool (``cast_to_chromecast_async`` / ``connect_to_chromecast_async``
        / ``_run_off_thread_result``), so a bare ``self.active_cast = dev``
        there is an unsynchronized cross-thread write: every transport/read
        path — pause/seek/volume routing here, the player backend's
        ``_cast_active`` poll — reads ``active_cast`` on the GUI thread. It's
        GIL-benign (a single pointer store), but unordered against those
        reads. Marshal via ``call_on_gui`` so the assignment, and the paired
        ``_cast_paused`` reset for the protocols that track it, always land
        on the GUI thread.

        ``call_on_gui`` runs the setter **inline when already on the GUI
        thread** (AirPlay's synchronous QEventLoop path, and the direct-call
        unit tests) and **queues it from a worker thread** — so the
        "arms ``active_cast`` on success" contract every caller relies on is
        preserved, just made thread-safe. The queued post is FIFO-ordered
        ahead of ``run_async``'s ``on_result`` (emitted after the worker fn
        returns), so ``active_cast`` is armed before ``on_done`` fires."""

        def _set():
            self.active_cast = dev
            if reset_paused:
                self._cast_paused = False

        from jellytoast.async_io import call_on_gui

        call_on_gui(_set)

    # ── Common ──────────────────────────────────────────────────────────────

    def discover_all(self):
        """Fan discovery across all five protocols. Each ``discover_*``
        is independently gated by its own ``cast/<type>_enabled`` toggle
        (and its optional-dep probe), so calling them all unconditionally
        here is safe — a disabled or unavailable protocol no-ops."""
        self.discover_chromecasts()
        self.discover_airplay()
        self.discover_dlna()
        self.discover_sonos()
        self.discover_snapcast()

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
        return (
            self.chromecast_devices
            + self.airplay_devices
            + self.dlna_devices
            + self.sonos_devices
            + self.snapcast_devices
        )

    def stop_cast(self):
        if not self.active_cast:
            return
        # Hand the device back its pre-cast volume while the connection is
        # still live (chromecast_stop quit_app's it). Device volume is
        # device-level and persists past quit_app, so this sticks.
        self._restore_device_volume()
        kind = self.active_cast.device_type
        if kind == CastType.CHROMECAST:
            self.chromecast_stop()
        elif kind == CastType.DLNA:
            self.dlna_stop()
        elif kind == CastType.SONOS:
            self.sonos_stop()
        elif kind == CastType.SNAPCAST:
            self.snapcast_stop()
        else:
            self.airplay_stop()
        # Expire the proxy's stream tokens now the session is over so a
        # leaked token can't be replayed against the relay. Best-effort +
        # read the module global so we don't *start* a proxy just to clear
        # it (a direct-cast session never created one).
        try:
            from jellytoast.cast_proxy import _PROXY

            if _PROXY is not None:
                _PROXY.clear_tokens()
        except Exception:
            pass

    # ── Transport dispatch (mid-cast play/pause + volume + seek) ─────────
    # Route by device_type, mirroring stop_cast. Chromecast is local + fast
    # so it runs inline; DLNA/Sonos block on SOAP so they run off the GUI
    # thread. Without this, the player's transport controls reached only the
    # chromecast_* methods (early-return off-Chromecast) and silently no-op'd.

    def _run_off_thread(self, fn):
        from jellytoast.async_io import run_async

        run_async(fn)

    def sync_cast_paused(self, paused: bool) -> None:
        """Track an externally-observed pause state — e.g. the user paused
        a DLNA/Sonos renderer from the device's OWN remote. Keeps
        ``_cast_paused`` (which drives ``cast_toggle_pause`` for DLNA/Sonos)
        in sync with reality, so the next in-app toggle isn't inverted
        (sending pause to an already-paused device → a dead first press)."""
        self._cast_paused = bool(paused)

    def cast_toggle_pause(self):
        """Play/pause the active cast. Chromecast queries the receiver;
        DLNA/Sonos toggle a tracked flag (set False on cast start) since we
        drive their playback, so a query round-trip isn't worth it."""
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == CastType.CHROMECAST:
            self.chromecast_pause()
            return
        paused = getattr(self, "_cast_paused", False)
        if kind == CastType.DLNA:
            fn = self._dlna_resume if paused else self._dlna_pause
        elif kind == CastType.SONOS:
            fn = self._sonos_resume if paused else self._sonos_pause
        else:
            return  # AirPlay v1 / Snapcast: no transport here
        # Drive _cast_paused from the off-thread transport RESULT, not
        # optimistically: a failed SOAP pause/resume must NOT flip the tracked
        # flag, or the next toggle sends the wrong command (a dropped pause
        # leaves the flag "paused" → the next press resumes a device that's
        # still playing). _run_off_thread_result marshals the bool back via
        # run_async's GUI-pinned signaler (and maps a backend exception to
        # on_result(False)), so the flag write lands on the GUI thread.
        target = not paused

        def _on_result(ok: bool) -> None:
            if ok:
                self._cast_paused = target

        self._run_off_thread_result(fn, _on_result)

    def cast_set_volume(self, percent: int):
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == CastType.CHROMECAST:
            self.chromecast_set_volume(percent)
        elif kind == CastType.DLNA:
            self._run_off_thread(lambda: self._dlna_set_volume(percent))
        elif kind == CastType.SONOS:
            self._run_off_thread(lambda: self._sonos_set_volume(percent))

    def cast_set_initial_volume(self, percent: int) -> int:
        """Push a defined volume on cast connect so a renderer that
        remembered a stale level from a prior session — silent (looks
        like nothing connected) or full-blast (jarring) — doesn't
        surprise the user. Routes by ``device_type`` exactly like
        ``cast_set_volume`` (DLNA/Sonos off the GUI thread; SOAP blocks).
        Chromecast already did this via ``_CAST_INITIAL_VOLUME``; this
        extends the same guarantee to the URL-push backends. Returns the
        value actually applied so the caller can sync the volume slider
        (Sonos clamps up to its volume floor)."""
        if not self.active_cast:
            return percent
        kind = self.active_cast.device_type
        # Chromecast GROUPS: do NOT force a single master volume. Forcing
        # `percent` on the group lands every speaker there and then audibly
        # snaps to the user's saved per-speaker balance when the mixer
        # applies it. Instead snapshot each member's pre-cast volume (handed
        # back on disconnect) and apply the saved balance up front, off the
        # GUI thread. Report the group's CURRENT aggregate so the master
        # slider tracks reality instead of a value we never set.
        if kind == CastType.CHROMECAST and getattr(self.active_cast, "cast_type", "") == "group":
            # Per-member saved levels + the pre-cast snapshot are applied
            # BEFORE media in the cast path (prepare_group_volume_before_media),
            # so don't force a master volume here — just report the group's
            # current aggregate so the master slider tracks reality.
            cur = self.chromecast_get_volume()
            return cur if cur is not None else percent
        # Capture the device's CURRENT volume before we override it, so
        # stop_cast can hand it back on disconnect.
        self._snapshot_device_volume(kind)
        if kind == CastType.CHROMECAST:
            self.chromecast_set_volume(percent)
            return percent
        if kind == CastType.DLNA:
            self._run_off_thread(lambda: self._dlna_set_volume(percent))
            return percent
        if kind == CastType.SONOS:
            applied = self._sonos_initial_volume(percent)
            self._run_off_thread(lambda: self._sonos_set_volume(applied))
            return applied
        return percent  # AirPlay v1 / Snapcast: no volume surface

    def _snapshot_device_volume(self, kind) -> None:
        """Capture the device's pre-cast volume so ``stop_cast`` can
        restore it. Chromecast exposes it locally (the receiver status is
        already cached — no round-trip). DLNA/Sonos volume reads are
        blocking SOAP and stay a follow-up, so those keep today's
        behaviour (no device-volume restore)."""
        self._pre_cast_device_volume = None
        if kind == CastType.CHROMECAST:
            self._pre_cast_device_volume = self.chromecast_get_volume()

    def _restore_device_volume(self) -> None:
        """Push the snapshotted pre-cast volume back to the device before
        teardown — a TV speaker used for music shouldn't be left at our
        cast level for TV. Falls back to a uniform level if the pre-cast
        volume couldn't be read; no-op for device types we don't snapshot
        so DLNA/Sonos/AirPlay keep today's behaviour."""
        if not self.active_cast:
            return
        # Group cast: hand each member speaker back its pre-cast device
        # volume. The single-device volume_level restore below only covers
        # the group's aggregate, which leaves a member (e.g. a TV speaker)
        # stuck at the cast level. Member device volume is independent of the
        # group session, so these sets stick past the group's teardown.
        if getattr(self.active_cast, "cast_type", "") == "group":
            members = self._pre_cast_member_volumes
            self._pre_cast_member_volumes = None
            for m in members or []:
                self.set_member_volume_async(m["uuid"], m["volume"])
            return
        snap = self._pre_cast_device_volume
        self._pre_cast_device_volume = None
        if snap is None:
            # Only impose the fallback on types we actually snapshot.
            if self.active_cast.device_type != CastType.CHROMECAST:
                return
            snap = _CAST_VOLUME_RESTORE_FALLBACK
        self.cast_set_volume(int(snap))

    def cast_seek(self, sec: float):
        """Absolute seek (the position slider). seek_relative (skip buttons)
        stays Chromecast-only for now — DLNA's controller seeks relative +
        forward-clamped, so per-backend relative handling is a follow-up."""
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == CastType.CHROMECAST:
            self.chromecast_seek(sec)
        elif kind == CastType.DLNA:
            self._run_off_thread(lambda: self._dlna_seek_abs(sec))
        elif kind == CastType.SONOS:
            self._run_off_thread(lambda: self._sonos_seek_abs(sec))

    def cast_seek_relative(self, delta_sec: float):
        """Skip ± mid-cast. Reads the current position per backend and
        absolute-seeks to pos+delta (so backward skip works on all three)."""
        if not self.active_cast:
            return
        kind = self.active_cast.device_type
        if kind == CastType.CHROMECAST:
            cc = self.active_cast.cast_object
            if cc is not None:
                try:
                    pos = cc.media_controller.status.current_time or 0.0
                except Exception:
                    pos = 0.0
                self.chromecast_seek(max(0.0, pos + delta_sec))
        elif kind == CastType.DLNA:
            self._run_off_thread(lambda: self._dlna_seek_relative(delta_sec))
        elif kind == CastType.SONOS:
            self._run_off_thread(lambda: self._sonos_seek_relative(delta_sec))

    # ── Unified track-start dispatch ─────────────────────────────────────
    # ONE per-type routing surface for "send this track to the armed cast
    # device". Both dispatch sites — ``player_backend.MpvController.play``
    # (a new track starts while a cast target is armed: auto-advance / next /
    # queue play) and ``jellytoast.app._cast_to_device`` (the user picks a device
    # while a track is playing) — call this instead of each carrying its own
    # copy of the chromecast/airplay/dlna/sonos ladder (the 2026-06-01 AT-18
    # de-dup). The per-type payload build + threading is byte-for-byte the
    # behaviour those two sites had inline:
    #   • Chromecast — direct-play MIME pick (FLAC stays FLAC) with the
    #     320 kbps MP3 transcode fallback and the live-radio bypass; pushed
    #     off the GUI thread via ``cast_to_chromecast_async``.
    #   • DLNA / Sonos — DIDL push off the GUI thread (SOAP blocks) via
    #     ``run_async`` around ``cast_to_dlna`` / ``cast_to_sonos``.
    #   • AirPlay v1 — synchronous ``cast_to_airplay``; ``on_done`` fires
    #     inline (no async hop), exactly as both sites did before.
    #   • Snapcast — a control surface, never a stream sink, so it no-ops
    #     here (both call sites already divert it before reaching this).
    # Site-specific behaviour that is NOT shared stays at the call sites:
    # the connect-without-media path, AirPlay 2 pairing, the local-mpv stop,
    # and the differing ``on_done`` bookkeeping. ``resume_seconds`` lets the
    # device-pick site resume mid-track; the auto-advance site leaves it 0.0
    # (which matches the async chromecast default), and live radio always
    # starts at 0.0 regardless (unseekable).
    def start_track(self, dev, np, *, provider, resume_seconds=0.0, on_done=None):
        """Route ``np`` to the cast device ``dev`` per ``dev.device_type``.

        ``provider`` builds the Chromecast / DLNA transcode-fallback URLs —
        passed in (rather than imported) so the call site's exact provider
        singleton is used. ``on_done(ok: bool)`` fires when the push lands
        (on the GUI thread for the async backends; inline for AirPlay)."""

        def _done(ok: bool) -> None:
            if on_done:
                on_done(bool(ok))

        # Internet-radio queues carry an inline live stream URL (Icecast/
        # HLS/HTTP) and have no real audio item: the transcode fallback
        # would 404 the receiver into IDLE/ERROR, and the stream is
        # unseekable so any resume offset is dropped.
        is_radio_item = bool(np.raw and np.raw.get("streamUrl"))
        start_sec = 0.0 if is_radio_item else resume_seconds
        kind = dev.device_type

        if kind == CastType.CHROMECAST:
            # Pick the highest-quality URL + MIME the receiver can direct-
            # play (FLAC stays FLAC, etc.); fall back to a 320 kbps MP3
            # transcode for codecs it can't groove (ALAC/DSD/…).
            container = (np.raw.get("Container") if np.raw else "") or ""
            url = np.stream_url
            if is_radio_item:
                mime = _radio_mime_for(url)
            elif np.is_audio:
                mime = self.chromecast_audio_mime_for(container)
                if mime is None:
                    url = provider.get_audio_transcode_url(
                        np.item_id, max_bitrate_kbps=320, codec="mp3"
                    )
                    mime = "audio/mpeg"
            else:
                mime = None
            self.cast_to_chromecast_async(
                dev,
                url,
                np.title,
                np.thumb_url,
                is_audio=np.is_audio,
                content_type=mime,
                current_time=start_sec,
                is_live=is_radio_item,
                on_done=_done,
            )
            return
        if kind == CastType.DLNA:
            from jellytoast.cast_payload import dlna_meta_from_np, make_transcode_fn

            meta = dlna_meta_from_np(np)
            tfn = None if is_radio_item else make_transcode_fn(provider, np.item_id)
            self._run_off_thread_result(
                lambda: self.cast_to_dlna(
                    dev,
                    np.stream_url,
                    meta,
                    transcode_url_fn=tfn,
                    start_sec=start_sec,
                    is_live=is_radio_item,
                ),
                _done,
            )
            return
        if kind == CastType.SONOS:
            self._run_off_thread_result(
                lambda: self.cast_to_sonos(
                    dev,
                    np.stream_url,
                    title=np.title,
                    artist=np.subtitle,
                    album=np.album,
                    art_url=np.thumb_url,
                    is_live=is_radio_item,
                ),
                _done,
            )
            return
        if kind == CastType.SNAPCAST:
            # Control surface, not a stream sink — nothing to push. Defensive
            # guard against misrouting into the AirPlay fall-through below.
            return
        # AirPlay v1 stays synchronous; report inline.
        ok = self.cast_to_airplay(dev, np.stream_url, np.title)
        _done(ok)

    def _run_off_thread_result(self, fn, on_result):
        """``run_async`` variant that marshals the bool result back to the
        caller (DLNA/Sonos block on SOAP, so they run off the GUI thread).
        A backend exception degrades to ``on_result(False)`` — same failure
        contract both dispatch sites had inline."""
        from jellytoast.async_io import run_async

        run_async(fn, on_result=on_result, on_error=lambda _e: on_result(False))

    def cleanup(self):
        # On app exit: stop any active cast session so the receiver
        # doesn't keep playing after the controller is gone, then
        # disconnect from every known Chromecast (pychromecast holds
        # background socket threads that prevent a clean process exit
        # otherwise), then tear down zeroconf.
        try:
            self.stop_cast()
        except Exception as e:
            # Don't swallow silently — a hung receiver on shutdown is
            # diagnostic-worthy. Print is fine here; cleanup runs once
            # per process under aboutToQuit so a logger setup would
            # be overkill.
            logger.warning("cast cleanup: stop_cast failed — %r", e)
        for dev in list(self.chromecast_devices):
            cc = dev.cast_object
            if cc is None:
                continue
            try:
                cc.disconnect(blocking=False)
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.cancel()
            except Exception:
                pass
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
        # Tear down the DLNA backend worker loop, if it was ever spun
        # up — it hosts a long-lived asyncio loop thread that would
        # otherwise keep the process alive. Soft-imported + best-effort
        # so an install without the optional dep (or a session that
        # never opened the cast menu) is unaffected.
        try:
            from jellytoast.cast import dlna as _dlna

            _dlna.get_dlna_controller().stop()
        except Exception:
            pass
        # Tear down the local cast proxy's HTTP server thread, if it
        # was ever started this session.
        try:
            from jellytoast.cast_proxy import get_cast_proxy

            get_cast_proxy().stop()
        except Exception:
            pass
        # Tear down the Snapcast controller's asyncio loop thread if one
        # was ever created (the control dialog lazily connects). Like the
        # DLNA backend it hosts a long-lived loop thread; leaving it
        # running races interpreter teardown. Read the module global
        # directly so cleanup doesn't *create* a controller just to stop
        # it. Best-effort + soft import.
        try:
            from jellytoast.cast import snapcast as _snap

            if _snap._CONTROLLER is not None:
                _snap._CONTROLLER.shutdown()
        except Exception:
            pass
