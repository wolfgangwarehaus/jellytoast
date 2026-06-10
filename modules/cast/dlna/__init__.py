"""DLNA / UPnP-AV control point — third cast target alongside
Chromecast (``pychromecast``) and AirPlay 2 (``pyatv``).

This package is the split-up successor to the former
``modules/cast/dlna.py`` monolith. Public surface is preserved
verbatim: ``cast_manager.py`` and every other importer continues to
``from modules.cast.dlna import …`` unchanged, and the test suite's
underscore-prefixed helper imports resolve here too.

Submodule layout (one-way dependency arrow — ``controller`` depends on
everything, nothing depends back on ``controller``):

- ``_constants``  — SSDP / MIME / codec constants
- ``_settings``   — settings access helpers
- ``_models``     — ``DlnaDevice`` / ``TrackMetadata`` / ``PushDecision``
- ``didl``        — ``build_didl_lite`` + XML / duration / cover helpers
- ``codec``       — codec-fallback decision tree + availability probe
- ``discovery``   — SSDP discovery dedup helpers
- ``_loop``       — ``_DlnaLoopThread`` (the asyncio worker thread)
- ``controller``  — ``DlnaController`` state machine + singleton

Backend-only slice for autonomous task **A22**. Discovers DLNA media
renderers via SSDP M-SEARCH, pushes streams via ``AVTransport``
``SetAVTransportURI`` + ``Play``, drives transport
(pause/stop/seek/volume), and polls ``GetTransportInfo`` +
``GetPositionInfo`` once a second so the existing ``PlayerBus`` queue-
advance machinery can wake on a track-end event. No UI wiring — the
``CastManager`` integration lands in the follow-up UI slice
(see ``docs/research/casting_dlna.md`` §12-§13).

The design doc — ``docs/research/casting_dlna.md`` — is the authority
for *why* this looks the way it does. Brief recap of the load-bearing
decisions, because someone will read this before that doc:

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
  can claim the same loop. See ``_loop.py``.

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

from ._constants import SSDP_ST_MEDIA_RENDERER, USER_AGENT_TEMPLATE
from ._loop import _DlnaLoopThread
from ._models import DlnaDevice, PushDecision, TrackMetadata, TranscodeUrlFn
from ._settings import (
    _settings_enabled,
    _settings_user_agent_overrides,
    _ua_for_device,
)
from .codec import (
    _ensure_async_upnp,
    decide_push_format,
    decide_retry_after_error,
    is_available,
)
from .controller import DlnaController, _td_to_sec, get_dlna_controller
from .didl import (
    _container_from_mime,
    _format_duration,
    _meta_with_mime,
    _protocol_info_for,
    _truncate_cover_url,
    _xml_attr,
    _xml_text,
    build_didl_lite,
)
from .discovery import (
    _parse_udn_from_usn,
    dedupe_search_response,
    parse_host_from_location,
)

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
    # Underscore-prefixed helpers re-exported deliberately: the split
    # preserves the flat-module import surface that tests/test_cast_dlna.py
    # patches and imports by full path. Listed here so the re-exports
    # read as intentional, not as dead imports.
    "TranscodeUrlFn",
    "_DlnaLoopThread",
    "_settings_enabled",
    "_settings_user_agent_overrides",
    "_ua_for_device",
    "_ensure_async_upnp",
    "_td_to_sec",
    "_container_from_mime",
    "_format_duration",
    "_meta_with_mime",
    "_protocol_info_for",
    "_truncate_cover_url",
    "_xml_attr",
    "_xml_text",
    "_parse_udn_from_usn",
]
