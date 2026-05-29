"""Codec-fallback decision tree + ``async-upnp-client`` availability probe.

``decide_push_format`` / ``decide_retry_after_error`` are the
"what MIME do we push" logic; ``is_available`` / ``_ensure_async_upnp``
are the "can we even speak DLNA" predicate — of a piece with the
decision tree. Pure functions plus one cached soft-import.
"""

from __future__ import annotations

from typing import Optional

from ._constants import _MIME_BY_CONTAINER, _TRANSCODE_RETRY_ERRORS
from ._models import PushDecision

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


# ── Codec-fallback decision tree ────────────────────────────────────────────


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
            mime="audio/mpeg",
            transcode=True,
            transcode_bitrate=320,
            reason="force_transcode",
        )
    container_l = (container or "").lower().lstrip(".")
    mime = upstream_mime or _MIME_BY_CONTAINER.get(container_l) or "application/octet-stream"
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
        mime="audio/mpeg",
        transcode=True,
        transcode_bitrate=320,
        reason=f"retry_after_{error_code}",
    )


__all__ = [
    "_ensure_async_upnp",
    "is_available",
    "decide_push_format",
    "decide_retry_after_error",
]
