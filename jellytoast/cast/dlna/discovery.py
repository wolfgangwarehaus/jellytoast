"""SSDP discovery dedup helpers for the DLNA backend.

``dedupe_search_response`` / ``parse_host_from_location`` /
``_parse_udn_from_usn``. Pure functions — no state, no network.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


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


__all__ = [
    "_parse_udn_from_usn",
    "dedupe_search_response",
    "parse_host_from_location",
]
