"""Curated list of well-known internet radio stations.

Drives the Radio view's "Browse popular" picker. Each entry is a dict
with the same shape provider stations use — ``{name, streamUrl,
homePageUrl, description}`` — so adding one is a straight call into
``provider.create_internet_radio_station``.

The list is small and hand-picked rather than crowdsourced (no
Radio-Browser API integration yet). All URLs are direct streams
published by the broadcaster on their own listen / API pages as of
2026-05-19 — if one stops working it's almost certainly because the
station rotated their stream endpoint, which they do occasionally;
edit this file and the picker updates next session.

Categories live in the entries (``"category"``) so the dialog can
group rows visually without us having to keep a parallel ordering.
"""

from __future__ import annotations

from typing import Dict, List


# Hand-audited 2026-05-19. Format: dicts with the same key shape the
# provider CRUD surface returns, so the picker's "Add" handler can
# pass entries straight into ``create_internet_radio_station`` with
# zero translation.
POPULAR_STATIONS: List[Dict[str, str]] = [
    # ── SomaFM (San Francisco, listener-supported, multi-channel) ─────────
    {
        "name": "SomaFM Groove Salad",
        "streamUrl": "https://ice2.somafm.com/groovesalad-128-aac",
        "homePageUrl": "https://somafm.com/groovesalad/",
        "logoUrl": "https://somafm.com/img/groovesalad400.jpg",
        "description": "Ambient + downtempo beats",
        "category": "Ambient / Electronic",
    },
    {
        "name": "SomaFM Drone Zone",
        "streamUrl": "https://ice2.somafm.com/dronezone-128-aac",
        "homePageUrl": "https://somafm.com/dronezone/",
        "logoUrl": "https://somafm.com/img/dronezone400.jpg",
        "description": "Atmospheric textures, deep ambient",
        "category": "Ambient / Electronic",
    },
    {
        "name": "SomaFM Indie Pop Rocks!",
        "streamUrl": "https://ice2.somafm.com/indiepop-128-aac",
        "homePageUrl": "https://somafm.com/indiepop/",
        "logoUrl": "https://somafm.com/img/indiepop400.jpg",
        "description": "Indie pop classics + new finds",
        "category": "Indie / Alternative",
    },
    {
        "name": "SomaFM Secret Agent",
        "streamUrl": "https://ice2.somafm.com/secretagent-128-aac",
        "homePageUrl": "https://somafm.com/secretagent/",
        "logoUrl": "https://somafm.com/img/secretagent400.jpg",
        "description": "Spy-jazz, exotica, bachelor-pad lounge",
        "category": "Jazz / Lounge",
    },
    # ── Public + nonprofit broadcasters ──────────────────────────────────
    {
        "name": "KEXP 90.3 Seattle",
        "streamUrl": "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
        "homePageUrl": "https://kexp.org/",
        # apple-touch-icon convention — most sites ship one at 180×180
        # at this path. The image cache's on_error path means a 404
        # falls through gracefully without crashing the cover load.
        "logoUrl": "https://www.kexp.org/apple-touch-icon.png",
        "description": "Tastemaker indie / alternative DJs",
        "category": "Indie / Alternative",
    },
    {
        "name": "WFMU Freeform",
        "streamUrl": "https://stream0.wfmu.org/freeform-128k",
        "homePageUrl": "https://wfmu.org/",
        "logoUrl": "https://wfmu.org/apple-touch-icon.png",
        "description": "Long-running freeform community radio, NJ",
        "category": "Eclectic / Freeform",
    },
    # ── NTS (London + LA, two channels) ──────────────────────────────────
    {
        "name": "NTS Radio 1",
        "streamUrl": "https://stream-relay-geo.ntslive.net/stream",
        "homePageUrl": "https://www.nts.live/",
        "logoUrl": "https://www.nts.live/apple-touch-icon.png",
        "description": "Underground music + spoken word",
        "category": "Eclectic / Freeform",
    },
    {
        "name": "NTS Radio 2",
        "streamUrl": "https://stream-relay-geo.ntslive.net/stream2",
        "homePageUrl": "https://www.nts.live/",
        "logoUrl": "https://www.nts.live/apple-touch-icon.png",
        "description": "NTS's second channel — more leftfield",
        "category": "Eclectic / Freeform",
    },
    # ── Radio Paradise (multiple curated mixes) ──────────────────────────
    {
        "name": "Radio Paradise Main Mix",
        "streamUrl": "https://stream.radioparadise.com/aac-128",
        "homePageUrl": "https://radioparadise.com/",
        "logoUrl": "https://img.radioparadise.com/covers/l/rp_main.jpg",
        "description": "Eclectic mix — rock, electronica, world",
        "category": "Eclectic / Freeform",
    },
    {
        "name": "Radio Paradise Mellow Mix",
        "streamUrl": "https://stream.radioparadise.com/mellow-128",
        "homePageUrl": "https://radioparadise.com/",
        "logoUrl": "https://img.radioparadise.com/covers/l/rp_mellow.jpg",
        "description": "Lower-energy songs from the main library",
        "category": "Eclectic / Freeform",
    },
]


def logo_url_for_stream(stream_url: str) -> str:
    """Return the curated logo URL for a stream we recognise, or empty
    string if unknown. Used by the play path to seed
    ``QueueContext.source_icon`` so the bar can show the station's
    artwork before any ICY metadata arrives — and as a fallback when
    a per-track art lookup misses."""
    if not stream_url:
        return ""
    for preset in POPULAR_STATIONS:
        if preset.get("streamUrl") == stream_url:
            return preset.get("logoUrl") or ""
    return ""


def category_order() -> List[str]:
    """Stable display order for category headers in the picker. New
    categories added to ``POPULAR_STATIONS`` that aren't in this list
    fall through to the bottom of the dialog under their own header,
    so missing one here degrades gracefully."""
    return [
        "Indie / Alternative",
        "Eclectic / Freeform",
        "Ambient / Electronic",
        "Jazz / Lounge",
    ]
