"""Detect server-side scrobbling on Navidrome — the double-scrobble
guard from the design doc.

The Subsonic API itself does not expose per-user scrobble state. So
we drop into Navidrome's *native* API (the one its web UI uses):

1. ``POST /auth/login`` with ``{"username":..., "password":...}`` →
   JSON body carrying ``token`` (JWT) and ``id`` (the user's record id).
2. ``GET /api/user/{id}`` with header ``Authorization: Bearer <jwt>``
   → user record. Per-user scrobble linkage shows up as fields whose
   exact names have varied across Navidrome versions; we treat *any
   non-empty* value across a small set of name variants as "linked".

Coupling caveat: this is undocumented Navidrome internals, not a
spec. The detector returns ``Result(detected=False)`` on every failure
shape (HTTP error, JSON shape change, missing fields) so callers can
fall back to the warning banner with no behaviour regression.

Only Navidrome — not all Subsonic-compatible servers — exposes this
native API. The caller is expected to short-circuit on non-Navidrome
servers (see ``ServerInfo.product_name`` from the Subsonic probe).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests


_TIMEOUT_S = 6.0


@dataclass
class Result:
    """Outcome of a server-side-scrobbler probe.

    ``detected`` is True only when we *successfully read* the user
    record. The two ``server_*`` flags are then load-bearing. When
    ``detected`` is False the caller should fall back to the warning
    banner without acting on the bools.
    """

    detected: bool = False
    server_lastfm: bool = False
    server_listenbrainz: bool = False
    # Display-only — Navidrome surfaces the linked usernames in the user
    # record on some versions. May stay empty even on detected=True.
    lastfm_username: str = ""
    listenbrainz_username: str = ""


def detect(server_url: str, username: str, password: str) -> Result:
    """Best-effort: try to reach Navidrome's native API and read the
    user's profile. Synchronous + blocking — caller dispatches via
    ``modules.async_io.run_async``.

    Returns ``Result(detected=False)`` on any error path (network,
    auth, JSON shape, missing fields). The caller should treat that
    as "couldn't tell — keep the warning banner up".
    """
    if not server_url or not username or not password:
        return Result()
    base = server_url.rstrip("/")
    headers = {
        "User-Agent": "jellytoast/0.1.0",
        "Content-Type": "application/json",
    }

    # Step 1: JWT login.
    try:
        r = requests.post(
            f"{base}/auth/login",
            json={"username": username, "password": password},
            headers=headers, timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        return Result()
    if r.status_code != 200:
        return Result()
    try:
        body = r.json()
    except ValueError:
        return Result()
    if not isinstance(body, dict):
        return Result()
    jwt = body.get("token") or body.get("jwt") or ""
    user_id = body.get("id") or body.get("userId") or body.get("user_id") or ""
    if not jwt or not user_id:
        return Result()

    # Step 2: read the user's profile.
    auth_headers = {
        "User-Agent": "jellytoast/0.1.0",
        "Authorization": f"Bearer {jwt}",
    }
    try:
        r = requests.get(
            f"{base}/api/user/{user_id}",
            headers=auth_headers, timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        return Result()
    if r.status_code != 200:
        return Result()
    try:
        user = r.json()
    except ValueError:
        return Result()
    if not isinstance(user, dict):
        return Result()

    return Result(
        detected=True,
        server_lastfm=_any_nonempty(user, _LASTFM_FIELD_VARIANTS),
        server_listenbrainz=_any_nonempty(user, _LB_FIELD_VARIANTS),
        lastfm_username=_first_string(user, _LASTFM_USERNAME_VARIANTS),
        listenbrainz_username=_first_string(user, _LB_USERNAME_VARIANTS),
    )


# ── Field-name variants ───────────────────────────────────────────────────
# Navidrome's user record has used different naming conventions across
# versions for the per-user scrobble linkage. We accept *any* of these
# being non-empty as evidence the user has connected that service. New
# variants get added here as we encounter them — there's no spec to
# pin down the canonical name.

_LASTFM_FIELD_VARIANTS = (
    "lastFMApiKey",      # historical Navidrome name
    "lastFMSessionKey",  # less common alias
    "lastfm_session",    # snake_case fallback
    "lastFMSession",
)

_LB_FIELD_VARIANTS = (
    "listenBrainzToken",
    "listenbrainz_token",
    "listenBrainzApiKey",
)

_LASTFM_USERNAME_VARIANTS = (
    "lastFMUsername",
    "lastfm_username",
    "lastFMUser",
)

_LB_USERNAME_VARIANTS = (
    "listenBrainzUsername",
    "listenbrainz_username",
    "listenBrainzUser",
)


def _any_nonempty(d: dict, keys: tuple[str, ...]) -> bool:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, bool) and v:
            return True
    return False


def _first_string(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""
