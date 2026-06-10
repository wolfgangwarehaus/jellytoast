"""Dataclasses for the DLNA backend.

``DlnaDevice`` / ``TrackMetadata`` / ``PushDecision`` plus the
``TranscodeUrlFn`` type alias. Pure data — no Qt, no asyncio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


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


# Type alias for the "give me a transcoded version of this URL"
# callback the controller asks the caller for during a 714 retry.
# The caller (provider abstraction) knows whether it's Jellyfin
# (``MaxStreamingBitrate=…&Container=mp3``) or Subsonic
# (``bitrate=…&format=mp3``); this module just signals what bitrate.
TranscodeUrlFn = Callable[[str, int], str]


__all__ = [
    "DlnaDevice",
    "TrackMetadata",
    "PushDecision",
    "TranscodeUrlFn",
]
