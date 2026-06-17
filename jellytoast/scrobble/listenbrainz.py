"""ListenBrainz client — pasted-token submit-listens API.

Endpoints (relative to the configurable base URL, default
``https://api.listenbrainz.org``):

- ``GET /1/validate-token``      — confirm a token + resolve username
- ``POST /1/submit-listens``     — playing_now / single / import payloads

Auth: ``Authorization: Token <user-token>`` on every write.

All blocking ``requests`` calls live here as plain functions; callers
dispatch them onto the shared thread pool via ``jellytoast.async_io.run_async``
so the GUI thread never stalls on a network round-trip.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from jellytoast.version import __version__

logger = logging.getLogger(__name__)

# Default + safety net. Per-call ``base_url`` always wins so a self-hosted
# instance can be used; this is just the fallback if a caller passes "".
DEFAULT_BASE_URL = "https://api.listenbrainz.org"

# ListenBrainz documents a 1000-listen cap per submit-listens request;
# we'll never approach that in practice but the constant is cited by
# the queue's batch flush so the limit lives in one place.
MAX_LISTENS_PER_BATCH = 1000

# Keep the network slice tight — scrobbles are non-critical and we don't
# want to wedge the pool worker if ListenBrainz is having a bad day.
_TIMEOUT_S = 10.0


def _normalize_base(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip()
    if not base:
        base = DEFAULT_BASE_URL
    return base.rstrip("/")


def _auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "User-Agent": f"jellytoast/{__version__} (+https://github.com/wolfgangwarehaus/jellytoast)",
    }


# ── Token validation ────────────────────────────────────────────────────────


def validate_token(token: str, base_url: str = "") -> Optional[str]:
    """Return the resolved username on success, or ``None`` on auth /
    network failure. Called from the settings page's Validate button.

    The endpoint returns ``{"valid": true, "user_name": "...", ...}``
    on success, ``{"valid": false, ...}`` for a bad token. We treat
    any non-2xx + any "valid": false the same — caller just sees None
    and surfaces a generic "couldn't validate" message."""
    if not token:
        return None
    url = f"{_normalize_base(base_url)}/1/validate-token"
    try:
        r = requests.get(url, headers=_auth_headers(token), timeout=_TIMEOUT_S)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not body.get("valid"):
        return None
    name = body.get("user_name") or ""
    return name or None


# ── Listen submission ───────────────────────────────────────────────────────


def build_track_metadata(
    artist_name: str,
    track_name: str,
    release_name: str = "",
    duration_ms: int = 0,
    recording_mbid: str = "",
) -> Dict[str, Any]:
    """Construct the ``track_metadata`` blob shared by playing_now and
    single-listen payloads. Only ``artist_name`` and ``track_name`` are
    required by the API — everything else is best-effort enrichment."""
    additional: Dict[str, Any] = {
        "submission_client": "jellytoast",
        "submission_client_version": __version__,
    }
    if duration_ms > 0:
        additional["duration_ms"] = int(duration_ms)
    if recording_mbid:
        additional["recording_mbid"] = recording_mbid
    payload: Dict[str, Any] = {
        "artist_name": artist_name,
        "track_name": track_name,
        "additional_info": additional,
    }
    if release_name:
        payload["release_name"] = release_name
    return payload


def send_now_playing(token: str, track_metadata: Dict[str, Any], base_url: str = "") -> bool:
    """Send a ``playing_now`` ping at track start. No ``listened_at`` —
    ListenBrainz uses now-playing as a transient "what's on" signal."""
    if not token:
        return False
    body = {
        "listen_type": "playing_now",
        "payload": [{"track_metadata": track_metadata}],
    }
    return _post_listens(token, body, base_url)


def send_single_listen(
    token: str, track_metadata: Dict[str, Any], listened_at: int, base_url: str = ""
) -> bool:
    """Submit one completed listen. ``listened_at`` is UNIX seconds UTC,
    typically the wall-clock at track *start* (per the ListenBrainz
    convention — start time, not finish time)."""
    if not token:
        return False
    if listened_at <= 0:
        listened_at = int(time.time())
    body = {
        "listen_type": "single",
        "payload": [
            {
                "listened_at": int(listened_at),
                "track_metadata": track_metadata,
            }
        ],
    }
    return _post_listens(token, body, base_url)


def send_listens_batch(token: str, listens: List[Dict[str, Any]], base_url: str = "") -> bool:
    """Submit a batch of completed listens. Each entry must already
    carry ``listened_at`` + ``track_metadata`` (the queue stores them
    in this shape so the flush path is just a passthrough). Sends as
    ``listen_type: import`` — the bulk-submit type ListenBrainz uses
    for offline-queue style flushes."""
    if not token or not listens:
        return False
    # Defensive cap — caller is responsible for chunking, but we never
    # want to push past the documented limit.
    if len(listens) > MAX_LISTENS_PER_BATCH:
        listens = listens[:MAX_LISTENS_PER_BATCH]
    body = {"listen_type": "import", "payload": listens}
    return _post_listens(token, body, base_url)


def _post_listens(token: str, body: Dict[str, Any], base_url: str) -> bool:
    url = f"{_normalize_base(base_url)}/1/submit-listens"
    try:
        r = requests.post(url, headers=_auth_headers(token), json=body, timeout=_TIMEOUT_S)
    except requests.RequestException:
        return False
    # ListenBrainz returns 200 + ``{"status": "ok"}`` on success.
    if 200 <= r.status_code < 300:
        return True
    # Permanent client errors (400 malformed / 422 unprocessable) will NEVER
    # succeed on retry — report "consumed" so the manager DROPS the batch
    # instead of re-queuing it forever (the queue scans the same poisoned
    # entry every flush otherwise). Transient failures (429 rate-limit, 5xx,
    # network) stay False so they retry on the next flush.
    if r.status_code in (400, 422):
        logger.warning(
            "ListenBrainz permanently rejected a listen batch (HTTP %s) — dropping it",
            r.status_code,
        )
        return True
    return False
