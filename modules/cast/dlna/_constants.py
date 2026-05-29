"""SSDP / MIME / codec constants for the DLNA backend.

Pure data — no Qt, no asyncio, no state. Imported by every other
submodule in the ``modules.cast.dlna`` package.
"""

from __future__ import annotations

from typing import Dict

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
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "wave": "audio/wav",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
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


__all__ = [
    "SSDP_ST_MEDIA_RENDERER",
    "USER_AGENT_TEMPLATE",
    "_DLNA_PN_BY_MIME",
    "_MIME_BY_CONTAINER",
    "_TRANSCODE_RETRY_ERRORS",
    "_DIDL_MAX_BYTES",
    "_DIDL_COVER_MAX_CHARS",
    "_POLL_INTERVAL_SEC",
]
