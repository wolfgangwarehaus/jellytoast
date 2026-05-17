"""DLNA / UPnP-AV control point — third cast target alongside
Chromecast (``pychromecast``) and AirPlay 2 (``pyatv``).

Backend-only slice for autonomous task **A22**. Discovers DLNA media
renderers via SSDP M-SEARCH, pushes streams via ``AVTransport``
``SetAVTransportURI`` + ``Play``, drives transport
(pause/stop/seek/volume), and polls ``GetTransportInfo`` +
``GetPositionInfo`` once a second so the existing ``PlayerBus`` queue-
advance machinery can wake on a track-end event. No UI wiring — the
``CastManager`` integration lands in the follow-up UI slice with august
(see ``docs/research/casting_dlna.md`` §12-§13).

The design doc — ``docs/research/casting_dlna.md`` — is the authority
for *why* this looks the way it does. Brief recap of the load-bearing
decisions, because someone will read this file before that doc:

- **``async-upnp-client``**.  Home Assistant's DLNA-DMR backbone; the
  only library where (a) the SSDP / SOAP / GENA parsers have survived a
  million HA boxes, (b) ``DmrDevice`` wraps AVTransport + RenderingControl
  so we don't hand-roll SOAP, (c) it's still on roughly-monthly releases
  in 2026. Soft-imported at first use — cold import pulls aiohttp +
  defusedxml + voluptuous (~150 ms warm), tolerable on the lazy path.

- **Private asyncio loop on its own worker thread**.  jellytoast uses no
  asyncio elsewhere; ``modules/async_io.run_async`` (Qt thread pool) is
  the convention everywhere else. ``DlnaController`` owns a single
  long-lived ``threading.Thread`` running a private ``asyncio`` loop and
  schedules coroutines via ``asyncio.run_coroutine_threadsafe``. This is
  the documented exception to the "don't use ``threading.Thread`` for
  I/O" rule from the project memory: the worker isn't blocking the Qt
  thread, it's hosting an asyncio loop the rest of the module submits
  work to. A future qasync rewrite (or a second asyncio-shaped feature)
  can claim the same loop.

- **Cast-proxy is mandatory, not opt-in**.  DLNA renderers fetch only
  URLs they can route to: a TV on ``192.168.1.0/24`` won't reach Jellyfin
  behind Tailscale; Bose / Yamaha firmwares with hard-coded TLS roots
  won't load self-signed certs. Every push goes through
  ``modules.cast_proxy.resolve_cast_url``, same as Chromecast +
  AirPlay 2 — so DLNA inherits ``cast_stream_routing`` semantics for
  free (no new key).

- **714 retry, not feature-detect**.  Don't call ``GetProtocolInfo`` —
  half the firmwares lie and the responses are often megabytes of
  comma-separated MIME strings. Push native, watch for 714 (Illegal MIME)
  or 701 (Transition Not Available), republish with a server-side
  transcode (`MaxStreamingBitrate=320000&Container=mp3` for Jellyfin /
  `bitrate=320&format=mp3` for Subsonic). The provider transcode-URL
  builder is the *caller's* responsibility; this module only signals
  "the renderer refused, please give me a transcoded URL".

- **Poll, don't subscribe (v1)**.  GENA event subscriptions need an
  inbound listener port, fail under default KDE Wayland firewall configs
  + Flatpak sandboxes, and many cheap renderers silently drop them.
  ``GetTransportInfo`` + ``GetPositionInfo`` every 1 s is ~2 HTTP/sec
  and works on every renderer. GENA is a v2 problem.

- **No new signals**.  ``PlayerBus.cast_started`` / ``cast_stopped`` /
  ``cast_devices_updated`` are sufficient — DLNA is just another
  ``device_type``. The research doc §10 considered a
  ``cast_transport_state(str, str)`` for v2 buffering spinners; deferred.

- **No device-specific quirk workarounds**.  We have no hardware to
  validate against. The DIDL builder ships the minimal portable shape
  that any spec-conformant renderer accepts; the renderer-quirk table in
  the research doc §6 is documentation, not code. Quirks that bite real
  users get coded after a real bug report.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


log = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────


# SSDP search target. ``ssdp:all`` brings in routers / printers / NAS
# shares — too noisy. ``MediaRenderer:1`` catches every DLNA-DMR.
SSDP_ST_MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"

# UPnP UDA + DLNA-DOC compliance string. Some Samsung firmware revs drop
# cover-art fetches when the controller doesn't advertise DLNA-DOC.
USER_AGENT_TEMPLATE = "jellytoast/{ver} UPnP/1.0 DLNADOC/1.50"

# Codec → ``DLNA.ORG_PN`` mapping for the five mainstream profiles we
# emit. Strict renderers (older Samsungs, some Yamahas) key on the
# profile name, not the MIME alone. Containers outside this dict are
# pushed with MIME only and rely on the 714 fallback if the renderer
# refuses — modern renderers play happily without the profile.
_DLNA_PN_BY_MIME: Dict[str, str] = {
    "audio/mpeg": "MP3",
    "audio/L16": "LPCM",
    "audio/flac": "FLAC",
    "audio/x-flac": "FLAC",
    "audio/wav": "WAVE",
    "audio/x-wav": "WAVE",
    "audio/mp4": "AAC_ISO",
    "audio/aac": "AAC_ISO",
}

# Container → MIME for things our codec-fallback decision tree wants to
# reason about. Mirrors ``CastManager._CHROMECAST_AUDIO_MIME`` but keyed
# for DLNA's stricter rendering — Ogg / WebM go through the proxy with
# MIME only and rely on transcode-on-714.
_MIME_BY_CONTAINER: Dict[str, str] = {
    "mp3":  "audio/mpeg",
    "flac": "audio/flac",
    "wav":  "audio/wav",
    "wave": "audio/wav",
    "m4a":  "audio/mp4",
    "mp4":  "audio/mp4",
    "aac":  "audio/aac",
    "ogg":  "audio/ogg",
    "oga":  "audio/ogg",
    "opus": "audio/ogg",
    "webm": "audio/webm",
}

# Renderer error codes we treat as "codec mismatch — please transcode".
# 714 = Illegal MIME-Type;  701 = Transition Not Available (some
# renderers report a refused codec as 701 because they never made it
# past PROBE → STOPPED state).
_TRANSCODE_RETRY_ERRORS = frozenset({701, 714})

# DIDL byte cap. Bose SoundTouch rejects DIDL docs over 4 kB; emit a
# minimal-fields fallback above this size. Cover URL ≤ 200 chars is
# part of the same belt-and-braces. Configurable purely for tests.
_DIDL_MAX_BYTES = 4096
_DIDL_COVER_MAX_CHARS = 200

# Polling cadence for transport-state tracking. 1 s is the research-doc
# default; ~2 HTTP/sec is cheap and renderer-friendly.
_POLL_INTERVAL_SEC = 1.0


# ── Lazy imports (cold cost ~150 ms) ────────────────────────────────────────


_async_upnp_imported: Optional[bool] = None


def _ensure_async_upnp() -> bool:
    """True if ``async-upnp-client`` imports successfully.

    Lazy so the cost (aiohttp + defusedxml + voluptuous transitive
    imports, ~150 ms warm) only happens when the user actually opens
    the cast popup. Cached: the answer doesn't change mid-process."""
    global _async_upnp_imported
    if _async_upnp_imported is None:
        try:
            import async_upnp_client  # noqa: F401
            _async_upnp_imported = True
        except ImportError:
            _async_upnp_imported = False
    return bool(_async_upnp_imported)


def is_available() -> bool:
    """Public probe — does this host have the DLNA library?

    Mirrors the ``modules.airplay2.is_available`` shape so the eventual
    ``CastManager`` integration can gate the DLNA discovery row the
    same way it gates AirPlay 2."""
    return _ensure_async_upnp()


# ── Settings access (read-only here; UI follow-up handles writes) ───────────


def _settings_enabled() -> bool:
    """Honor ``cast/dlna_enabled`` (default True).

    Returning ``True`` when settings can't be read keeps the test path
    (no QSettings, no Qt) and the first-run path (key never written)
    aligned: DLNA on unless explicitly disabled."""
    try:
        from modules.settings import get_settings
        s = get_settings()
    except Exception:
        return True
    # QSettings stores ints; fall back to a defensive bool() so any
    # legacy string write still does the right thing.
    raw = s._s.value("cast/dlna_enabled", True)
    if isinstance(raw, str):
        return raw.lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def _settings_user_agent_overrides() -> Dict[str, str]:
    """Per-renderer User-Agent overrides — ``cast/dlna_user_agent_overrides``.

    JSON map of ``device-name pattern → User-Agent``. No UI today; this
    is the power-user escape hatch for the Samsung-firmware UA quirk
    documented in the research doc §6. Returns an empty dict on missing
    / malformed values so the caller can always iterate."""
    try:
        from modules.settings import get_settings
        s = get_settings()
    except Exception:
        return {}
    raw = s._s.value("cast/dlna_user_agent_overrides", "", type=str)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(v, dict):
        return {}
    # Reject non-string values defensively — QSettings can return
    # surprising types if the user hand-edits the conf file.
    return {str(k): str(val) for k, val in v.items() if val}


def _ua_for_device(device_name: str, version: str = "0.x") -> str:
    """Pick a User-Agent for a push, honoring per-device overrides.

    Pattern matching is substring (case-insensitive) — keeps the JSON
    config human-writable. First match wins; falls back to the global
    UA template if no override fires."""
    overrides = _settings_user_agent_overrides()
    if overrides and device_name:
        haystack = device_name.lower()
        for pattern, ua in overrides.items():
            if pattern and pattern.lower() in haystack:
                return ua
    return USER_AGENT_TEMPLATE.format(ver=version)


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class DlnaDevice:
    """A discovered DLNA renderer. Wraps just enough state for the cast
    picker to render the row + the controller to push a stream.

    ``udn`` is the UPnP unique identifier (``uuid:…``) — stable across
    reboots, used as the dedup key. ``location`` is the description-XML
    URL SSDP reported; ``device_obj`` is the ``DmrDevice`` once bound
    (lazy — only populated after the user actually picks the renderer,
    so discovery doesn't pay the description-fetch cost for every
    device on the LAN)."""

    name: str
    host: str
    port: int
    udn: str
    location: str
    manufacturer: str = ""
    model_name: str = ""
    device_obj: Any = field(default=None, repr=False)


@dataclass
class TrackMetadata:
    """Minimal portable shape for ``SetAVTransportURI``'s DIDL-Lite
    metadata argument. Mirrors the fields most renderers actually
    render; everything else is noise (research doc §6).

    ``mime`` is the upstream MIME (``audio/flac``, ``audio/mpeg``…).
    ``duration_sec`` is wall-clock seconds; the DIDL builder formats it
    as UPnP's ``H:MM:SS.mmm``. ``size_bytes`` is optional but a few
    older Pioneer / Onkyo receivers want a numeric ``size`` attribute
    to allocate a seek window."""

    item_id: str
    title: str
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: int = 0
    duration_sec: float = 0.0
    mime: str = ""
    size_bytes: int = 0
    cover_url: str = ""


# ── DIDL-Lite builder ───────────────────────────────────────────────────────


def _format_duration(duration_sec: float) -> str:
    """``H:MM:SS.mmm`` — UPnP AVTransport's spec format."""
    if duration_sec <= 0:
        return "0:00:00.000"
    total_ms = int(round(duration_sec * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def _protocol_info_for(mime: str) -> str:
    """Compose a ``protocolInfo`` for the ``<res>`` element.

    Shape: ``http-get:*:<mime>:<DLNA.ORG_PN>;DLNA.ORG_OP=01`` — the
    ``OP=01`` flag advertises seekable-via-Range so renderers offer
    a position bar. The profile name is the load-bearing bit for
    strict renderers; if we don't have one (Ogg, WebM), emit the
    minimal MIME form and let the 714 fallback do the work."""
    mime_l = (mime or "application/octet-stream").lower()
    pn = _DLNA_PN_BY_MIME.get(mime_l) or _DLNA_PN_BY_MIME.get(mime)
    base = f"http-get:*:{mime_l}:"
    if pn:
        return f"{base}DLNA.ORG_PN={pn};DLNA.ORG_OP=01"
    return f"{base}*"


def _xml_attr(s: str) -> str:
    """XML-attribute-safe escape for ``protocolInfo`` etc."""
    return html.escape(s or "", quote=True)


def _xml_text(s: str) -> str:
    """XML-text-content escape (no quote-attr handling)."""
    return html.escape(s or "", quote=False)


def _truncate_cover_url(url: str) -> str:
    """Bose-friendly cover-URL cap.

    Bose SoundTouch rejects DIDL > 4 kB, and a long token URL is the
    easiest part of the document to bloat. Drop the cover URL entirely
    if it's over the cap rather than hand the renderer a half-truncated
    URL that 404s — most TVs are unbothered by a missing cover."""
    if not url or len(url) <= _DIDL_COVER_MAX_CHARS:
        return url
    return ""


def build_didl_lite(meta: TrackMetadata, stream_url: str) -> str:
    """Build a minimal, portable DIDL-Lite document for
    ``SetAVTransportURI``'s second argument.

    The renderer parses this for title / artist / album / cover; skip
    it and most TVs render "Unknown", some Sony / Bose models refuse
    the URI entirely. ``upnp:class`` is mandatory — Sony Bravia and
    some Yamaha rev firmwares return 714 without it.

    Output is the *unescaped* XML document. The SOAP layer
    (``async_upnp_client``) re-escapes the whole string when it
    interpolates it into the SOAP envelope; double-escaping here would
    show up on the receiver as ``&amp;lt;`` rubble."""
    # Belt-and-braces cover-URL cap, then a size-driven fallback for
    # the still-too-big case (e.g. a very long Subsonic auth blob).
    cover = _truncate_cover_url(meta.cover_url)
    duration = _format_duration(meta.duration_sec)
    proto = _protocol_info_for(meta.mime)

    # Build the cover-art element conditionally — emitting an empty
    # tag is worse than omitting it on cheap TVs (some render a broken-
    # image icon for an empty src).
    cover_xml = ""
    if cover:
        cover_xml = (
            f'    <upnp:albumArtURI dlna:profileID="JPEG_TN">'
            f'{_xml_text(cover)}</upnp:albumArtURI>\n'
        )

    track_no = ""
    if meta.track_number > 0:
        track_no = (
            f"    <upnp:originalTrackNumber>{int(meta.track_number)}"
            f"</upnp:originalTrackNumber>\n"
        )

    artist_xml = ""
    if meta.artist:
        artist_xml = (
            f"    <dc:creator>{_xml_text(meta.artist)}</dc:creator>\n"
            f"    <upnp:artist>{_xml_text(meta.artist)}</upnp:artist>\n"
        )
    if meta.album_artist and meta.album_artist != meta.artist:
        artist_xml += (
            f'    <upnp:artist role="AlbumArtist">'
            f"{_xml_text(meta.album_artist)}</upnp:artist>\n"
        )

    album_xml = ""
    if meta.album:
        album_xml = (
            f"    <upnp:album>{_xml_text(meta.album)}</upnp:album>\n"
        )

    res_attrs = [
        f'protocolInfo="{_xml_attr(proto)}"',
        f'duration="{duration}"',
    ]
    if meta.size_bytes > 0:
        res_attrs.append(f'size="{int(meta.size_bytes)}"')
    res_attr_str = " ".join(res_attrs)

    item_id_safe = _xml_attr(meta.item_id or "jt-1")

    doc = (
        '<DIDL-Lite '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">\n'
        f'  <item id="{item_id_safe}" parentID="0" restricted="1">\n'
        f"    <dc:title>{_xml_text(meta.title or 'Unknown')}</dc:title>\n"
        f"{artist_xml}"
        f"{album_xml}"
        f"{track_no}"
        f"{cover_xml}"
        "    <upnp:class>object.item.audioItem.musicTrack</upnp:class>\n"
        f"    <res {res_attr_str}>{_xml_text(stream_url)}</res>\n"
        "  </item>\n"
        "</DIDL-Lite>"
    )

    # If the full document still busts the 4 kB cap (cover dropped but
    # an enormous title / artist string survived), strip non-essential
    # fields one-by-one until it fits. This is the Bose-cap fallback.
    if len(doc.encode("utf-8")) > _DIDL_MAX_BYTES:
        # Strip everything except title + res + class.
        doc = (
            '<DIDL-Lite '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">\n'
            f'  <item id="{item_id_safe}" parentID="0" restricted="1">\n'
            f"    <dc:title>{_xml_text(meta.title or 'Unknown')}</dc:title>\n"
            "    <upnp:class>object.item.audioItem.musicTrack</upnp:class>\n"
            f"    <res {res_attr_str}>{_xml_text(stream_url)}</res>\n"
            "  </item>\n"
            "</DIDL-Lite>"
        )
    return doc


# ── Codec-fallback decision tree ────────────────────────────────────────────


@dataclass
class PushDecision:
    """The result of asking ``decide_push_format`` what MIME to push
    with — flat dataclass so tests can assert without dict-key churn.

    ``transcode`` is True when the caller should ask the server for an
    MP3 re-encode (Jellyfin ``MaxStreamingBitrate=320000&Container=mp3``
    or Subsonic ``bitrate=320&format=mp3``); the upstream URL builder
    is the caller's job — this module just decides *whether*."""

    mime: str
    transcode: bool
    transcode_bitrate: int = 0  # kbps; 0 when transcode == False
    reason: str = ""


def decide_push_format(
    container: str,
    *,
    force_transcode: bool = False,
    upstream_mime: str = "",
) -> PushDecision:
    """First-pass MIME / transcode decision for a fresh push.

    Strategy (research doc §7):

    1. Caller-supplied ``upstream_mime`` wins if non-empty.
    2. Else lookup ``container`` in the MIME table.
    3. ``force_transcode`` short-circuits straight to MP3 / 320 kbps.
    4. Unknown containers still try a push (MIME ``application/
       octet-stream``); the 714 fallback handles refusal."""
    if force_transcode:
        return PushDecision(
            mime="audio/mpeg", transcode=True, transcode_bitrate=320,
            reason="force_transcode",
        )
    container_l = (container or "").lower().lstrip(".")
    mime = (upstream_mime
            or _MIME_BY_CONTAINER.get(container_l)
            or "application/octet-stream")
    return PushDecision(mime=mime, transcode=False, reason="native")


def decide_retry_after_error(
    error_code: Optional[int],
    container: str,
) -> Optional[PushDecision]:
    """Decide whether a failed ``SetAVTransportURI`` warrants a retry.

    ``error_code`` is the UPnP error number from a SOAP fault. None
    when we couldn't parse one out — treated as a non-retryable
    network / parse failure to avoid masking real bugs with a
    transcode that papers over a stream URL the renderer can't reach.

    Returns the new ``PushDecision`` to use for the retry, or ``None``
    when the error doesn't match a known transcode-worthy code."""
    if error_code is None:
        return None
    if error_code not in _TRANSCODE_RETRY_ERRORS:
        return None
    return PushDecision(
        mime="audio/mpeg", transcode=True, transcode_bitrate=320,
        reason=f"retry_after_{error_code}",
    )


# ── Discovery dedup ─────────────────────────────────────────────────────────


def _parse_udn_from_usn(usn: str) -> str:
    """Extract the bare ``uuid:…`` UDN from an SSDP ``USN`` header.

    USN forms:  ``uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1``
                ``uuid:abc::upnp:rootdevice``  ``uuid:abc``  Returns
    ``uuid:abc`` in every case; ``""`` if the header is malformed."""
    if not usn:
        return ""
    head = usn.split("::", 1)[0].strip()
    return head if head.lower().startswith("uuid:") else ""


def dedupe_search_response(
    response: Dict[str, str],
    seen: Dict[str, str],
) -> Optional[Tuple[str, str]]:
    """Filter a single SSDP M-SEARCH response against an in-flight
    ``seen`` map.  ``response`` is the case-insensitive header dict
    yielded by ``async_search``'s callback (``USN``, ``LOCATION``,
    ``ST``…).  ``seen`` is ``{udn: location}``.

    Returns ``(udn, location)`` when the response is novel (caller
    should fetch the description), ``None`` when it's a duplicate or
    malformed. Mutates ``seen`` in-place — the caller doesn't need to
    track which keys it added."""
    usn = response.get("usn") or response.get("USN") or ""
    udn = _parse_udn_from_usn(usn)
    location = response.get("location") or response.get("LOCATION") or ""
    if not udn or not location:
        return None
    if udn in seen:
        return None
    # Some renderers report multiple URLs (rootdevice + MediaRenderer
    # service); pin the first description URL we see and ignore later
    # duplicates from the same UDN.
    seen[udn] = location
    return udn, location


def parse_host_from_location(location: str) -> Tuple[str, int]:
    """Extract ``(host, port)`` from an SSDP ``LOCATION`` URL.

    Returns ``("", 0)`` on parse failure — the discovery row still gets
    populated (UDN is the dedup key) but the picker won't have a
    LAN-routable hostname to display in the tooltip."""
    if not location:
        return "", 0
    parsed = urlparse(location)
    host = parsed.hostname or ""
    port = parsed.port or 0
    if port == 0 and parsed.scheme == "http":
        port = 80
    return host, port


# ── Asyncio loop worker ─────────────────────────────────────────────────────


class _DlnaLoopThread:
    """Owns a private asyncio loop on a dedicated daemon thread.

    The thread is long-lived (one per process; idle when no DLNA work
    is in flight) and the loop's run/stop lifecycle is fully managed
    here so callers only see the synchronous ``submit`` /
    ``submit_blocking`` API. This is the documented exception to the
    project's "no ``threading.Thread`` for I/O" rule — see module
    docstring."""

    def __init__(self, name: str = "jellytoast-dlna"):
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True,
            )
            self._thread.start()
            # Block briefly until the loop has actually been bound —
            # avoids a race where a fast submit() outruns loop creation
            # and crashes on ``run_coroutine_threadsafe(loop=None)``.
            self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    def is_running(self) -> bool:
        return (self._thread is not None
                and self._thread.is_alive()
                and self._loop is not None
                and not self._loop.is_closed())

    def submit(self, coro: Awaitable) -> "asyncio.Future":
        """Schedule ``coro`` on the loop. Returns a concurrent.futures
        ``Future`` (cross-thread-safe). Caller uses ``.result(timeout)``
        from another thread, or attaches an ``add_done_callback``."""
        if not self.is_running():
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def submit_blocking(self, coro: Awaitable, timeout: float = 30.0) -> Any:
        """Convenience: submit and block for the result. Don't call from
        the asyncio loop's own thread (would deadlock); the assertion
        guards the common case."""
        fut = self.submit(coro)
        return fut.result(timeout=timeout)

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None or thread is None:
                return
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            thread.join(timeout=timeout)
            self._loop = None
            self._thread = None


# ── DlnaController ──────────────────────────────────────────────────────────


# Type alias for the "give me a transcoded version of this URL"
# callback the controller asks the caller for during a 714 retry.
# The caller (provider abstraction) knows whether it's Jellyfin
# (``MaxStreamingBitrate=…&Container=mp3``) or Subsonic
# (``bitrate=…&format=mp3``); this module just signals what bitrate.
TranscodeUrlFn = Callable[[str, int], str]


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
    ) -> List[DlnaDevice]:
        """Synchronous SSDP M-SEARCH. Blocks the caller for ``timeout``
        seconds (run from a worker thread, not the GUI thread).

        ``on_device`` fires for each *new* renderer as the SSDP
        responses arrive; the returned list is the complete deduped
        snapshot once the search window closes."""
        if not _ensure_async_upnp():
            return []
        if not _settings_enabled():
            return []
        self._loop_thread.start()
        return self._loop_thread.submit_blocking(
            self.async_discover(timeout=timeout, on_device=on_device),
            timeout=float(timeout) + 5.0,
        )

    async def async_discover(
        self,
        timeout: int = 5,
        on_device: Optional[Callable[[DlnaDevice], None]] = None,
    ) -> List[DlnaDevice]:
        """Async sibling of ``discover``. Returns the deduped list."""
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
        return found

    def known_devices(self) -> List[DlnaDevice]:
        with self._lock:
            return list(self._devices.values())

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
                    dev, stream_url, meta,
                    transcode_url_fn=transcode_url_fn,
                    force_transcode=force_transcode,
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
    ) -> bool:
        """Async sibling of ``play``. Implements the 714-retry decision
        tree end-to-end (decide → push → on-error retry)."""
        dmr = await self.async_bind(dev)
        if dmr is None:
            return False

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
                log.warning("DLNA transcode_url_fn raised: %s; "
                            "falling back to native URL", e)
            attempt_meta = _meta_with_mime(meta, decision.mime)

        didl = build_didl_lite(attempt_meta, url)

        ok, err_code = await self._try_set_and_play(dmr, url, didl)
        if ok:
            self._mark_active(dev, dmr)
            return True

        retry = decide_retry_after_error(err_code, _container_from_mime(meta.mime))
        if retry is None or transcode_url_fn is None:
            return False

        # 714 fallback. Pin the renderer to transcode for the rest of
        # the session; the UI follow-up persists this into
        # ``cast/dlna_force_transcode`` after a real success.
        self._transcode_cache[dev.udn] = True
        try:
            retry_url = transcode_url_fn(stream_url, retry.transcode_bitrate)
        except Exception as e:  # noqa: BLE001
            log.warning("DLNA retry transcode_url_fn raised: %s", e)
            return False
        retry_meta = _meta_with_mime(meta, retry.mime)
        retry_didl = build_didl_lite(retry_meta, retry_url)
        ok, _ = await self._try_set_and_play(dmr, retry_url, retry_didl)
        if ok:
            self._mark_active(dev, dmr)
        return ok

    async def _try_set_and_play(
        self,
        dmr: Any,
        url: str,
        didl: str,
    ) -> Tuple[bool, Optional[int]]:
        """One SetAVTransportURI + Play attempt. Returns
        ``(ok, error_code_or_None)``."""
        from async_upnp_client.exceptions import (  # lazy
            UpnpActionResponseError, UpnpError,
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
            _format_duration(max(0.0, seconds)),
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


# ── Helpers (module-level so tests can hit them without instantiation) ──────


def _container_from_mime(mime: str) -> str:
    """Best-effort reverse-lookup. Returns ``""`` on unknown MIME — the
    decision tree treats that as "let the upstream MIME drive"."""
    if not mime:
        return ""
    mime_l = mime.lower()
    # Pick the first container whose canonical MIME maps to ``mime``.
    for container, m in _MIME_BY_CONTAINER.items():
        if m == mime_l:
            return container
    return ""


def _meta_with_mime(meta: TrackMetadata, new_mime: str) -> TrackMetadata:
    """Return a copy of ``meta`` with ``mime`` replaced. Used during the
    714 retry to swap ``audio/flac`` → ``audio/mpeg`` without mutating
    the caller's dataclass."""
    return TrackMetadata(
        item_id=meta.item_id, title=meta.title, artist=meta.artist,
        album=meta.album, album_artist=meta.album_artist,
        track_number=meta.track_number, duration_sec=meta.duration_sec,
        mime=new_mime, size_bytes=0, cover_url=meta.cover_url,
    )


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
    "DlnaDevice",
    "PushDecision",
    "SSDP_ST_MEDIA_RENDERER",
    "TrackMetadata",
    "USER_AGENT_TEMPLATE",
    "build_didl_lite",
    "decide_push_format",
    "decide_retry_after_error",
    "dedupe_search_response",
    "get_dlna_controller",
    "is_available",
    "parse_host_from_location",
]
