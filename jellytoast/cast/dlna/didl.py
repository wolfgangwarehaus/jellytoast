"""DIDL-Lite builder + MIME helpers for the DLNA backend.

``build_didl_lite`` plus the private XML / duration / cover helpers
and ``_container_from_mime`` / ``_meta_with_mime``. Pure functions —
no state, no Qt, no asyncio.
"""

from __future__ import annotations

import html

from ._constants import (
    _DIDL_COVER_MAX_CHARS,
    _DIDL_MAX_BYTES,
    _DLNA_PN_BY_MIME,
    _MIME_BY_CONTAINER,
)
from ._models import TrackMetadata


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
            f"{_xml_text(cover)}</upnp:albumArtURI>\n"
        )

    track_no = ""
    if meta.track_number > 0:
        track_no = (
            f"    <upnp:originalTrackNumber>{int(meta.track_number)}</upnp:originalTrackNumber>\n"
        )

    artist_xml = ""
    if meta.artist:
        artist_xml = (
            f"    <dc:creator>{_xml_text(meta.artist)}</dc:creator>\n"
            f"    <upnp:artist>{_xml_text(meta.artist)}</upnp:artist>\n"
        )
    if meta.album_artist and meta.album_artist != meta.artist:
        artist_xml += (
            f'    <upnp:artist role="AlbumArtist">{_xml_text(meta.album_artist)}</upnp:artist>\n'
        )

    album_xml = ""
    if meta.album:
        album_xml = f"    <upnp:album>{_xml_text(meta.album)}</upnp:album>\n"

    res_attrs = [
        f'protocolInfo="{_xml_attr(proto)}"',
        f'duration="{duration}"',
    ]
    if meta.size_bytes > 0:
        res_attrs.append(f'size="{int(meta.size_bytes)}"')
    res_attr_str = " ".join(res_attrs)

    item_id_safe = _xml_attr(meta.item_id or "jt-1")

    doc = (
        "<DIDL-Lite "
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
            "<DIDL-Lite "
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
        item_id=meta.item_id,
        title=meta.title,
        artist=meta.artist,
        album=meta.album,
        album_artist=meta.album_artist,
        track_number=meta.track_number,
        duration_sec=meta.duration_sec,
        mime=new_mime,
        size_bytes=0,
        cover_url=meta.cover_url,
    )


__all__ = [
    "build_didl_lite",
    "_format_duration",
    "_protocol_info_for",
    "_xml_attr",
    "_xml_text",
    "_truncate_cover_url",
    "_container_from_mime",
    "_meta_with_mime",
]
