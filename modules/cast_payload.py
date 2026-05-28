"""Shared cast-payload helpers.

The two cast dispatch sites — ``jellytoast.JellytoastWindow._cast_to_device``
(the user picks a device) and ``player_backend.MpvController.play`` (a new
track starts while a cast target is armed) — both need to turn the current
``NowPlaying`` into the argument shape a backend transport call expects.
Centralising that here keeps the two sites from drifting apart (the audit
flagged the Chromecast prep as already duplicated between them).

Scope: the URL-push backends that take DIDL-style metadata — **DLNA** and
**Sonos**. Chromecast keeps its own inline MIME prep (its direct-play story
differs from DLNA's push-native-then-714-retry), and Snapcast is a control
surface, not a URL push, so neither routes through here.
"""

from __future__ import annotations

from typing import Callable


def dlna_meta_from_np(np) -> "object":
    """Build a DLNA ``TrackMetadata`` from a ``NowPlaying``.

    ``mime`` is derived from the source container so the renderer gets a
    *native* push first; an empty/unknown container leaves ``mime`` blank
    and the controller's 714-retry + ``transcode_url_fn`` handle a refusal
    (see ``modules/cast/dlna`` package docstring)."""
    # Imported lazily — pulling the dlna package at module-import time would
    # drag the (optional) UPnP backend in for callers that never cast DLNA.
    from modules.cast.dlna import TrackMetadata
    from modules.cast.dlna._constants import _MIME_BY_CONTAINER

    raw = np.raw or {}
    container = (raw.get("Container") or "").lower().lstrip(".")
    mime = _MIME_BY_CONTAINER.get(container, "")
    return TrackMetadata(
        item_id=np.item_id,
        title=np.title,
        artist=np.subtitle,
        album=np.album,
        album_artist=raw.get("AlbumArtist", "") or np.subtitle,
        track_number=int(raw.get("IndexNumber", 0) or 0),
        duration_sec=(np.duration or 0) / 1000.0,
        mime=mime,
        cover_url=np.thumb_url,
    )


def make_transcode_fn(provider, item_id: str) -> Callable[[str, int], str]:
    """Provider-side transcode-URL builder for the DLNA 714 fallback:
    ``(original_url, bitrate_kbps) -> mp3_url``. The controller calls this
    only when a renderer rejects the native MIME (UPnP 714/701)."""

    def _fn(_original_url: str, bitrate_kbps: int) -> str:
        return provider.get_audio_transcode_url(
            item_id, max_bitrate_kbps=bitrate_kbps, codec="mp3"
        )

    return _fn
