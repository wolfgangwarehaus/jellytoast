"""Per-track cover-art lookup for internet-radio ICY titles.

When an Icecast/Shoutcast stream's ``icy-title`` metadata lands we have
the artist + track name but no album art — the audio stream itself
doesn't carry images. This module turns the title text into a cover
URL by:

1. Parsing the ICY title into ``(artist, title)``. The common format
   is ``Artist - Title``; we also handle the three-segment NTS-style
   ``Show - Artist - Title`` by taking the last two segments.
2. Querying MusicBrainz's recording search for that artist + title.
3. Asking the Cover Art Archive for the front cover of the first
   release on the top-scoring recording.

If anything misses — bad title, MB returns nothing, CAA has no art —
the helper returns ``None`` and the caller's fallback (the station
logo) takes over.

Threading: the public ``lookup_art_url`` is **blocking** and is meant
to be called from a ``modules.async_io.run_async`` worker, not the GUI
thread. The 1-req/sec MusicBrainz rate limit is enforced module-local
so two concurrent lookups serialize cleanly.

Caching: an in-memory LRU keyed by ``(artist_lower, title_lower)``
maps to ``Optional[str]`` (a URL or the sentinel-None for misses), so
a station that loops through a 30-track playlist hits the network
only once per unique track. Cap is small (256) because radio sessions
are bounded.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)


# ── Module constants ──────────────────────────────────────────────────────


_MB_BASE = "https://musicbrainz.org/ws/2"
_CAA_BASE = "https://coverartarchive.org"
# MusicBrainz requires a descriptive User-Agent with contact info per
# their access policy. The contact email mirrors the one in pyproject.
_USER_AGENT = "jellytoast/2026.5.19 ( augustvontrips@gmail.com )"
# MB rate-limit: 1 request per second per IP for anonymous use. We
# enforce it with a single module-wide lock + last-call timestamp so
# concurrent lookups serialize cleanly. CAA is much more permissive
# and we hit it only once per successful MB recording match anyway.
_MB_MIN_INTERVAL_S = 1.05
_TIMEOUT_S = 6
# LRU cap — radio sessions are bounded and so is unique-track turnover.
# 256 covers a long listening session without unbounded growth.
_CACHE_CAP = 256

# Score threshold for a MusicBrainz recording match to be considered
# "good enough" to look up cover art for. MB returns a 0-100 score
# based on match confidence; 70 strikes a balance between coverage
# and false positives (mainstream tracks score 90-100; obscure /
# fuzzy matches drop below 70).
_MIN_MB_SCORE = 70


# ── Cache + rate-limit (module-private state) ─────────────────────────────


_cache: "dict[Tuple[str, str], Optional[str]]" = {}
_cache_order: list = []  # insertion-ordered keys for LRU eviction
_cache_lock = threading.Lock()

_mb_lock = threading.Lock()
_mb_last_call_t: float = 0.0


def _cache_get(key: Tuple[str, str]) -> "Tuple[bool, Optional[str]]":
    """Returns ``(hit, value)``. ``hit=False`` means the key wasn't
    cached at all (do a network lookup). ``hit=True`` with ``None``
    means "we looked it up and there's no art" — don't re-query."""
    with _cache_lock:
        if key in _cache:
            return True, _cache[key]
        return False, None


def _cache_put(key: Tuple[str, str], value: Optional[str]) -> None:
    with _cache_lock:
        if key in _cache:
            _cache[key] = value
            return
        _cache[key] = value
        _cache_order.append(key)
        while len(_cache_order) > _CACHE_CAP:
            old = _cache_order.pop(0)
            _cache.pop(old, None)


def _clear_cache_for_tests() -> None:
    """Test hook — drop every cached entry. Callers in the live app
    have no reason to ever clear this; the LRU bound is small enough
    that it's self-managing."""
    with _cache_lock:
        _cache.clear()
        _cache_order.clear()


# ── Public API ────────────────────────────────────────────────────────────


def parse_icy_title(raw: str) -> Tuple[str, str]:
    """Best-effort split of an ICY title into ``(artist, title)``.

    Recognised shapes (from most-common in practice to least):
    ``"Artist - Title"`` → trivial.
    ``"Show - Artist - Title"`` (NTS / SomaFM specials) → take the
    last two segments.
    Anything without a separator → return ``("", raw)`` so the caller
    can decide whether to try a title-only lookup.

    All segments are stripped of leading / trailing whitespace; an
    empty input returns ``("", "")``."""
    if not raw:
        return "", ""
    s = raw.strip()
    if not s:
        return "", ""
    # Split on " - " (space-dash-space) — the canonical ICY separator.
    # Plain "-" without spaces is too aggressive (it appears inside
    # song titles like "Re-Animator").
    parts = [p.strip() for p in s.split(" - ") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    # Three or more segments — assume "Show - Artist - Title" and take
    # the last two. Stations that violate this (rare) get a lower hit
    # rate; the fallback is the station logo, which is fine.
    return parts[-2], parts[-1]


def lookup_art_url(artist: str, title: str) -> Optional[str]:
    """Resolve a cover-art URL for the given track. Returns ``None``
    when no match exists or any network step fails — the caller falls
    back to the station logo.

    **Blocking** — call from a ``run_async`` worker. The MB rate
    limit (1 req/sec) is enforced internally so two concurrent
    lookups serialize without the caller having to coordinate."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    # A title-only lookup is too ambiguous on MB ("Yesterday" matches
    # thousands of recordings); require both fields.
    if not artist or not title:
        return None

    key = (artist.lower(), title.lower())
    hit, cached = _cache_get(key)
    if hit:
        return cached

    try:
        mbid = _search_release_mbid(artist, title)
    except Exception as e:
        logger.warning("MB search failed for %r / %r: %s", artist, title, e)
        _cache_put(key, None)
        return None

    if not mbid:
        logger.debug("no MB match for %r / %r", artist, title)
        _cache_put(key, None)
        return None

    try:
        art_url = _resolve_caa_front(mbid)
    except Exception as e:
        logger.warning("CAA lookup failed for mbid %s: %s", mbid, e)
        _cache_put(key, None)
        return None

    if art_url:
        logger.debug("cover for %r / %r → %s", artist, title, art_url)
    else:
        logger.debug("no CAA art for %r / %r (mbid %s)", artist, title, mbid)
    _cache_put(key, art_url)
    return art_url


# ── Internal helpers ──────────────────────────────────────────────────────


def _respect_mb_rate_limit() -> None:
    """Block until at least ``_MB_MIN_INTERVAL_S`` has elapsed since
    the last MB call across the whole process. Holds the lock for the
    duration so concurrent callers queue cleanly."""
    global _mb_last_call_t
    _mb_lock.acquire()
    try:
        now = time.monotonic()
        delta = now - _mb_last_call_t
        wait = _MB_MIN_INTERVAL_S - delta
        if wait > 0:
            time.sleep(wait)
        _mb_last_call_t = time.monotonic()
    finally:
        _mb_lock.release()


def _search_release_mbid(artist: str, title: str) -> Optional[str]:
    """Query MusicBrainz for ``artist`` + ``title``; pick the highest-
    scoring recording at or above ``_MIN_MB_SCORE`` and return the
    first release-mbid attached to it. ``None`` if nothing qualifies."""
    # MB Lucene query syntax: backslash-escape the few characters that
    # break the parser, then quote phrases so multi-word fields match
    # exactly. Simple version — keeps the query short enough that MB's
    # 500-char limit is never a concern.
    def _q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    query = f'recording:"{_q(title)}" AND artist:"{_q(artist)}"'
    url = (
        f"{_MB_BASE}/recording/"
        f"?query={quote_plus(query)}&fmt=json&limit=5"
    )
    _respect_mb_rate_limit()
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=_TIMEOUT_S,
    )
    if resp.status_code != 200:
        return None
    data = resp.json() if resp.content else {}
    recordings = data.get("recordings") or []
    # MB sorts by score desc; double-check + filter below the threshold.
    for rec in recordings:
        try:
            score = int(rec.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < _MIN_MB_SCORE:
            continue
        releases = rec.get("releases") or []
        for rel in releases:
            mbid = rel.get("id")
            if mbid:
                return str(mbid)
    return None


def _resolve_caa_front(release_mbid: str) -> Optional[str]:
    """Ask Cover Art Archive whether ``release_mbid`` has front cover
    art. Returns the URL of the 500-px thumbnail if so, else ``None``.

    CAA's release endpoint returns JSON listing every image attached
    to a release; ``/release/{mbid}/front-500`` is a 307 redirect to
    the thumbnail when one exists, or 404 when none does — that's the
    cheap path. We follow the redirect and use the resolved URL so
    callers can pass it straight into the image cache without re-
    redirecting on every paint."""
    url = f"{_CAA_BASE}/release/{release_mbid}/front-500"
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT_S,
        allow_redirects=True,
    )
    if resp.status_code == 200 and resp.url:
        return str(resp.url)
    return None
