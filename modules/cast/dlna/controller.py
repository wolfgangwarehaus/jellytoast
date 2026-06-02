"""``DlnaController`` — the DLNA backend's state machine.

Lifecycle, discovery, renderer binding, push (with the 714-retry
decision tree), transport control, and 1 s state polling. Kept whole:
the ``_lock``-guarded six-field state machine (``_devices``,
``_active_udn``, ``_active_device_obj``, ``_transcode_cache``,
``_last_state``, ``_poll_task``) doesn't decompose cleanly.

Dependency direction: this module depends on every other submodule in
``modules.cast.dlna``; nothing depends back on it.

NOTE on the availability / settings gates: ``_ensure_async_upnp`` and
``_settings_enabled`` are resolved through the package namespace
(``modules.cast.dlna``) at call time rather than imported directly.
The test suite monkeypatches those names on the package module, and
this indirection keeps the patches load-bearing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._constants import _POLL_INTERVAL_SEC, SSDP_ST_MEDIA_RENDERER
from ._loop import _DlnaLoopThread
from ._models import DlnaDevice, TrackMetadata, TranscodeUrlFn
from .codec import decide_push_format, decide_retry_after_error
from .didl import _container_from_mime, _meta_with_mime, build_didl_lite
from .discovery import dedupe_search_response, parse_host_from_location

log = logging.getLogger(__name__)


def _ensure_async_upnp() -> bool:
    """Resolve ``_ensure_async_upnp`` via the package namespace.

    The test suite patches ``modules.cast.dlna._ensure_async_upnp``;
    going through the package module here keeps that patch effective."""
    from modules.cast import dlna as _pkg

    return bool(_pkg._ensure_async_upnp())


def _settings_enabled() -> bool:
    """Resolve ``_settings_enabled`` via the package namespace.

    Same indirection rationale as :func:`_ensure_async_upnp`."""
    from modules.cast import dlna as _pkg

    return bool(_pkg._settings_enabled())


def _td_to_sec(td: Any) -> float:
    """Coerce a ``timedelta`` (or already-numeric) into seconds. Returns
    0.0 on None / parse failure — saves a tower of ``if x is not None``
    checks in callers."""
    if td is None:
        return 0.0
    if isinstance(td, (int, float)):
        return float(td)
    # timedelta.total_seconds() is the canonical path.
    fn = getattr(td, "total_seconds", None)
    if callable(fn):
        try:
            return float(fn())
        except Exception:
            return 0.0
    return 0.0


class DlnaController:
    """Top-level facade for the rest of the app.

    One per process. Owns the asyncio loop thread + the discovered-
    device cache + the active-renderer state. Methods are synchronous
    (they ``submit_blocking`` under the hood) so the existing
    ``CastManager`` call-site shape — ``cast_to_*`` returns ``bool`` —
    stays valid in the UI follow-up.

    The async surface is also exposed (``async_*`` siblings) so future
    asyncio-shaped consumers can re-use the loop without a sync hop.
    """

    def __init__(self):
        self._loop_thread = _DlnaLoopThread()
        self._devices: Dict[str, DlnaDevice] = {}
        self._active_udn: Optional[str] = None
        self._active_device_obj: Any = None
        self._lock = threading.Lock()
        # Per-renderer transcode-required flag, populated on first 714.
        # Persisted into ``cast/dlna_force_transcode`` by the UI layer;
        # this in-memory cache is the session-scoped read.
        self._transcode_cache: Dict[str, bool] = {}
        # Last sampled transport state for the active renderer — fed by
        # the 1 s poll, read by the queue-advance check.
        self._last_state: Dict[str, Any] = {}
        self._poll_task: Optional[Any] = None  # asyncio.Task

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the loop thread. Idempotent."""
        if not _ensure_async_upnp():
            log.warning("async-upnp-client not installed; DLNA disabled")
            return
        if not _settings_enabled():
            log.info("DLNA disabled via settings; not starting loop")
            return
        self._loop_thread.start()

    def stop(self) -> None:
        """Tear down — cancels the poll, stops the loop thread.

        Doesn't unsubscribe GENA (we don't subscribe in v1) and doesn't
        force-stop the renderer (that's the user's job — leaving the
        cast running after we exit is a feature). The transcode cache
        is dropped; persistence lives in the UI layer."""
        self._cancel_poll_locked()
        self._loop_thread.stop()
        with self._lock:
            self._devices.clear()
            self._active_udn = None
            self._active_device_obj = None
            self._last_state.clear()

    # ── Discovery ───────────────────────────────────────────────────────────

    def discover(
        self,
        timeout: int = 5,
        on_device: Optional[Callable[[DlnaDevice], None]] = None,
        validate: bool = False,
    ) -> List[DlnaDevice]:
        """Synchronous SSDP M-SEARCH. Blocks the caller for ``timeout``
        seconds (run from a worker thread, not the GUI thread).

        ``on_device`` fires for each *new* renderer as the SSDP
        responses arrive; the returned list is the complete deduped
        snapshot once the search window closes.

        ``validate=True`` binds each candidate's description before
        returning it and drops any that aren't a real bindable DMR — see
        ``async_discover``."""
        if not _ensure_async_upnp():
            return []
        if not _settings_enabled():
            return []
        self._loop_thread.start()
        return self._loop_thread.submit_blocking(
            self.async_discover(timeout=timeout, on_device=on_device, validate=validate),
            # Validation binds each device (one description fetch apiece,
            # run in parallel) — give the blocking wait extra headroom.
            timeout=float(timeout) + (20.0 if validate else 5.0),
        )

    async def async_discover(
        self,
        timeout: int = 5,
        on_device: Optional[Callable[[DlnaDevice], None]] = None,
        validate: bool = False,
    ) -> List[DlnaDevice]:
        """Async sibling of ``discover``. Returns the deduped list (or,
        with ``validate=True``, the deduped list filtered to renderers
        that actually bind as a DMR)."""
        from async_upnp_client.search import async_search  # lazy

        seen: Dict[str, str] = {}
        found: List[DlnaDevice] = []

        async def _cb(headers) -> None:
            # ``headers`` is a CaseInsensitiveDict from async-upnp-client.
            # Convert to plain dict so our dedup helper can be tested
            # without dragging the CaseInsensitiveDict class in.
            try:
                resp = {k.lower(): str(v) for k, v in headers.items()}
            except AttributeError:
                resp = dict(headers)
            r = dedupe_search_response(resp, seen)
            if not r:
                return
            udn, location = r
            host, port = parse_host_from_location(location)
            # SSDP USN gives us the UDN; the friendly name lives in the
            # description XML. We populate the name lazily (when the
            # caller actually picks the renderer) to keep discovery
            # cheap. Until then the row labels itself by host.
            dev = DlnaDevice(
                name=host or udn,
                host=host,
                port=port,
                udn=udn,
                location=location,
            )
            with self._lock:
                self._devices[udn] = dev
            found.append(dev)
            if on_device:
                try:
                    on_device(dev)
                except Exception as e:  # noqa: BLE001
                    log.warning("DLNA on_device callback raised: %s", e)

        try:
            await async_search(
                async_callback=_cb,
                timeout=int(timeout),
                search_target=SSDP_ST_MEDIA_RENDERER,
            )
        except Exception as e:  # noqa: BLE001 - SSDP is best-effort
            log.warning("DLNA SSDP search failed: %s", e)
        if not validate:
            return found
        # Validate each candidate by binding its description. A device
        # that answers the MediaRenderer M-SEARCH but isn't actually a
        # bindable DMR — a combo media-server, a router's UPnP IGD, etc.
        # (e.g. the 192.168.x.x box found alongside an LG TV on
        # 2026-05-28) — would otherwise clutter the picker and fail only
        # at cast time. Binding here also warms the DmrDevice cache + sets
        # the friendly name, so a validated device casts faster and the
        # row shows a real name instead of the bare IP. Bind in parallel
        # so total latency is the slowest single fetch, not the sum.
        results = await asyncio.gather(
            *(self.async_bind(d) for d in found), return_exceptions=True
        )
        validated: List[DlnaDevice] = []
        for d, r in zip(found, results, strict=False):
            if isinstance(r, Exception) or r is None:
                with self._lock:
                    self._devices.pop(d.udn, None)
                log.debug("DLNA discovery: dropped non-renderer %s (%s)", d.host, d.udn)
                continue
            validated.append(d)
        return validated

    # ── Renderer binding ───────────────────────────────────────────────────

    async def async_bind(self, dev: DlnaDevice) -> Any:
        """Fetch the description XML for ``dev`` and bind a
        ``DmrDevice`` we can drive. Cached on the ``DlnaDevice``
        instance so a re-pick doesn't re-fetch.

        Returns the ``DmrDevice`` (truthy) or ``None`` on failure."""
        if dev.device_obj is not None:
            return dev.device_obj
        # All async-upnp-client imports stay local to async paths so the
        # cold-import cost (aiohttp + voluptuous + defusedxml ~150 ms)
        # only fires when the user actually triggers a cast.
        from async_upnp_client.aiohttp import AiohttpRequester
        from async_upnp_client.client_factory import UpnpFactory
        from async_upnp_client.profiles.dlna import DmrDevice

        requester = AiohttpRequester()
        factory = UpnpFactory(requester)
        try:
            upnp_device = await factory.async_create_device(dev.location)
        except Exception as e:  # noqa: BLE001
            log.warning("DLNA bind failed for %s: %s", dev.location, e)
            return None
        dmr = DmrDevice(upnp_device, event_handler=None)
        # Fill the friendly name + manufacturer fields the description
        # XML carries — these populate the picker tooltip.
        try:
            dev.name = dmr.name or dev.name
            dev.manufacturer = dmr.manufacturer or ""
            dev.model_name = dmr.model_name or ""
        except Exception:
            pass
        dev.device_obj = dmr
        return dmr

    # ── Push ───────────────────────────────────────────────────────────────

    def play(
        self,
        dev: DlnaDevice,
        stream_url: str,
        meta: TrackMetadata,
        *,
        transcode_url_fn: Optional[TranscodeUrlFn] = None,
        force_transcode: bool = False,
        start_sec: float = 0.0,
    ) -> bool:
        """Push ``stream_url`` to the renderer and start playback.

        ``stream_url`` *must* already be a cast-proxy URL — the caller
        is responsible for funneling raw provider URLs through
        ``modules.cast_proxy.resolve_cast_url`` before reaching this
        method, mirroring the Chromecast / AirPlay 2 call sites.

        ``transcode_url_fn(original_url, bitrate_kbps) -> new_url`` is
        the provider-side fallback path: when the renderer rejects the
        native MIME (UPnP 714 / 701), this controller asks the caller
        for a transcoded URL and re-pushes once.

        Returns True if the renderer reports a non-stopped state after
        the push, False otherwise (caller surfaces the failure)."""
        if not _ensure_async_upnp():
            return False
        self._loop_thread.start()
        try:
            return self._loop_thread.submit_blocking(
                self.async_play(
                    dev,
                    stream_url,
                    meta,
                    transcode_url_fn=transcode_url_fn,
                    force_transcode=force_transcode,
                    start_sec=start_sec,
                ),
                timeout=30.0,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("DLNA play failed: %s", e)
            return False

    async def async_play(
        self,
        dev: DlnaDevice,
        stream_url: str,
        meta: TrackMetadata,
        *,
        transcode_url_fn: Optional[TranscodeUrlFn] = None,
        force_transcode: bool = False,
        start_sec: float = 0.0,
    ) -> bool:
        """Async sibling of ``play``. Implements the 714-retry decision
        tree end-to-end (decide → push → on-error retry).

        ``start_sec`` resumes playback at that offset (best-effort seek
        once the renderer is playing) so casting a track that was already
        underway locally doesn't restart it from 0."""
        dmr = await self.async_bind(dev)
        if dmr is None:
            return False

        # Reset to STOPPED before loading the new URI. LG webOS (and other
        # picky renderers) return UPnP 701 "Transition not available" on
        # SetAVTransportURI once the transport already has media loaded,
        # and won't reliably auto-Play a freshly-set URI until it's been
        # reset. Best-effort — an already-stopped transport may itself
        # error, which is fine. (Verified live against a real LG TV on
        # 2026-05-28: without this the first push loaded the player but
        # never auto-played and the next push got 701; with it the TV
        # reports PLAYING with the position advancing. The 714 retry below
        # reuses this now-stopped transport, so it needn't re-stop. The
        # DLNA research doc §6 anticipated coding renderer quirks "after a
        # real bug report" — this is that report.)
        try:
            await dmr.async_stop()
        except Exception:
            pass

        # Cached transcode pin: if a previous push to this UDN already
        # tripped the 714 fallback, skip the native attempt this time.
        already_pinned = self._transcode_cache.get(dev.udn, False)
        decision = decide_push_format(
            container=_container_from_mime(meta.mime),
            force_transcode=force_transcode or already_pinned,
            upstream_mime=meta.mime,
        )

        url, attempt_meta = stream_url, meta
        if decision.transcode and transcode_url_fn is not None:
            try:
                url = transcode_url_fn(stream_url, decision.transcode_bitrate)
            except Exception as e:  # noqa: BLE001
                log.warning("DLNA transcode_url_fn raised: %s; falling back to native URL", e)
            attempt_meta = _meta_with_mime(meta, decision.mime)

        didl = build_didl_lite(attempt_meta, url)

        ok, err_code = await self._try_set_and_play(dmr, url, didl)
        if not ok:
            # Native push errored. Try the 714/701 transcode fallback FIRST —
            # a genuine format rejection needs the re-push, not a state poll.
            retry = decide_retry_after_error(err_code, _container_from_mime(meta.mime))
            if retry is not None and transcode_url_fn is not None:
                # Pin the renderer to transcode for the rest of the session;
                # the UI follow-up persists this into ``cast/dlna_force_
                # transcode`` after a real success.
                self._transcode_cache[dev.udn] = True
                try:
                    retry_url = transcode_url_fn(stream_url, retry.transcode_bitrate)
                    retry_meta = _meta_with_mime(meta, retry.mime)
                    retry_didl = build_didl_lite(retry_meta, retry_url)
                    ok, _ = await self._try_set_and_play(dmr, retry_url, retry_didl)
                except Exception as e:  # noqa: BLE001
                    log.warning("DLNA retry transcode_url_fn raised: %s", e)
            # Last resort: some renderers (LG webOS) return a UPnP error on the
            # push yet DO transition to PLAYING — trust the real transport
            # state before declaring failure, so the GUI doesn't report "Cast
            # failed" while the TV is playing (audit #8; the docstring's stated
            # "non-stopped state" contract, now actually checked).
            if not ok and await self._renderer_started(dmr):
                log.info(
                    "DLNA push errored (%s) but renderer is playing — "
                    "treating as success",
                    err_code,
                )
                ok = True
        if ok:
            self._mark_active(dev, dmr)
            await self._maybe_resume_seek(dmr, start_sec)
            return True
        return False

    async def _renderer_started(self, dmr: Any) -> bool:
        """Did the renderer actually start, regardless of the push's SOAP
        response code? Polls the live transport state a few times (it can
        read TRANSITIONING for a moment right after Play). Returns True on
        any non-stopped state. Used to rescue the LG-webOS-style "errors on
        the push but plays anyway" case so the GUI doesn't report a failure
        for a cast that's audibly working."""
        for i in range(4):
            try:
                await dmr.async_update()
            except Exception:
                return False
            raw = getattr(dmr, "transport_state", None)
            state = str(getattr(raw, "value", raw) or "").upper()
            if any(tok in state for tok in ("PLAYING", "TRANSITIONING", "PAUSED")):
                return True
            if i < 3:
                await asyncio.sleep(0.4)
        return False

    async def _maybe_resume_seek(self, dmr: Any, start_sec: float) -> None:
        """Seek to ``start_sec`` right after a successful push so a cast
        of an already-playing track resumes where the user was. Best-
        effort: a renderer that's still BUFFERING/TRANSITIONING may reject
        the seek — then it just plays from 0 (the prior behaviour). The
        <1 s guard skips a no-op seek for a fresh-from-zero track."""
        if not start_sec or start_sec < 1.0:
            return
        try:
            await dmr.async_seek_rel_time(timedelta(seconds=float(start_sec)))
        except Exception as e:  # noqa: BLE001
            log.debug("DLNA resume-seek to %.1fs failed (renderer not ready?): %s", start_sec, e)

    async def _try_set_and_play(
        self,
        dmr: Any,
        url: str,
        didl: str,
    ) -> Tuple[bool, Optional[int]]:
        """One SetAVTransportURI + Play attempt. Returns
        ``(ok, error_code_or_None)``."""
        from async_upnp_client.exceptions import (  # lazy
            UpnpActionResponseError,
            UpnpError,
        )

        try:
            await dmr.async_set_transport_uri(url, "", didl)
            await dmr.async_play()
            return True, None
        except UpnpActionResponseError as e:
            code = getattr(e, "error_code", None)
            log.info("DLNA push error %s: %s", code, e)
            return False, int(code) if code is not None else None
        except UpnpError as e:
            log.info("DLNA push UpnpError: %s", e)
            return False, None
        except Exception as e:  # noqa: BLE001
            log.warning("DLNA push unexpected error: %s", e)
            return False, None

    def _mark_active(self, dev: DlnaDevice, dmr: Any) -> None:
        with self._lock:
            self._active_udn = dev.udn
            self._active_device_obj = dmr

    # ── Transport control ──────────────────────────────────────────────────

    def pause(self) -> bool:
        return self._dispatch_active("async_pause")

    def resume(self) -> bool:
        return self._dispatch_active("async_play")

    def stop_renderer(self) -> bool:
        ok = self._dispatch_active("async_stop")
        with self._lock:
            self._active_udn = None
            self._active_device_obj = None
            self._last_state.clear()
        return ok

    def seek(self, seconds: float) -> bool:
        return self._dispatch_active(
            "async_seek_rel_time",
            timedelta(seconds=max(0.0, seconds)),
        )

    def set_volume(self, percent: int) -> bool:
        level = max(0.0, min(1.0, percent / 100.0))
        return self._dispatch_active("async_set_volume_level", level)

    def set_mute(self, on: bool) -> bool:
        return self._dispatch_active("async_mute_volume", bool(on))

    def _dispatch_active(self, method_name: str, *args) -> bool:
        with self._lock:
            dmr = self._active_device_obj
        if dmr is None:
            return False
        method = getattr(dmr, method_name, None)
        if method is None:
            return False
        try:
            self._loop_thread.submit_blocking(method(*args), timeout=10.0)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("DLNA %s failed: %s", method_name, e)
            return False

    # ── State polling ──────────────────────────────────────────────────────

    def start_polling(
        self,
        on_state: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Begin the 1 s ``GetTransportInfo`` + ``GetPositionInfo``
        poll. ``on_state(state_dict)`` fires each tick with the
        flat state dict (keys: ``transport_state``, ``position_sec``,
        ``duration_sec``).

        NOTE: GENA event subscriptions (the spec-blessed alternative)
        are deferred to v2 — they need an inbound listener port that
        fails under default KDE Wayland firewall configs + Flatpak
        sandboxes. Polling works on every renderer."""
        self._loop_thread.start()
        self._cancel_poll_locked()
        with self._lock:
            self._poll_task = self._loop_thread.submit(self._poll_forever(on_state))

    def stop_polling(self) -> None:
        self._cancel_poll_locked()

    def _cancel_poll_locked(self) -> None:
        with self._lock:
            task = self._poll_task
            self._poll_task = None
        if task is None:
            return
        try:
            task.cancel()
        except Exception:
            pass

    async def _poll_forever(
        self,
        on_state: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        while True:
            try:
                state = await self._sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("DLNA poll error: %s", e)
                state = None
            if state is not None:
                with self._lock:
                    self._last_state = state
                if on_state is not None:
                    try:
                        on_state(state)
                    except Exception as e:  # noqa: BLE001
                        log.warning("DLNA on_state raised: %s", e)
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    async def _sample_once(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            dmr = self._active_device_obj
        if dmr is None:
            return None
        # async_update fetches both AVTransport state and RenderingControl
        # volume in one round-trip on async-upnp-client's high-level
        # DmrDevice wrapper. Renderers without RenderingControl just
        # return partial state — we tolerate missing fields.
        try:
            await dmr.async_update()
        except Exception:
            return None
        return {
            "transport_state": getattr(dmr, "transport_state", None) or "",
            "position_sec": _td_to_sec(getattr(dmr, "media_position", None)),
            "duration_sec": _td_to_sec(getattr(dmr, "media_duration", None)),
            "volume_level": getattr(dmr, "volume_level", None),
        }

    def last_state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_state)


# ── Module singleton ────────────────────────────────────────────────────────


_CONTROLLER: Optional[DlnaController] = None


def get_dlna_controller() -> DlnaController:
    """Lazy module-level singleton, mirroring ``get_cast_proxy``."""
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = DlnaController()
    return _CONTROLLER


__all__ = [
    "DlnaController",
    "get_dlna_controller",
    "_td_to_sec",
]
