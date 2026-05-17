"""Last.fm scrobbling client — desktop browser-auth + signed POSTs.

The two halves are split:

1. **Auth flow** (one-shot, runs from the settings page when the user
   clicks "Connect to Last.fm"):

   - ``get_token()`` → unauthorized request token
   - user opens ``https://www.last.fm/api/auth/?api_key=…&token=…`` in
     their browser, clicks "Allow"
   - ``get_session(token)`` → permanent session key + username

   The settings dialog drives the polling loop: after opening the
   browser, repeat ``get_session`` every couple of seconds until the
   user authorizes (success), or the user cancels (we just stop).

2. **Scrobble writes** (every track, from ScrobbleManager):

   - ``update_now_playing(session_key, ...)`` on track start
   - ``scrobble(session_key, ...)`` on completion (single)
   - ``scrobble_batch(session_key, listens)`` for queue flushes
     (≤50 listens per request — Last.fm's documented batch cap)

Every call needs an ``api_sig``: alphabetically sort all params
(except ``format`` and ``callback``), concatenate ``name+value`` pairs,
append the shared secret, MD5. Format=json on responses; the API
returns XML by default.

DEFERRED — needs the api_key + api_secret. Until ``IS_CONFIGURED`` is
True, the settings UI hides the Last.fm half. august registers an app
at ``last.fm/api/account/create`` and pastes the values into the two
constants below.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import requests


# ── Credentials ────────────────────────────────────────────────────────────
# Filled in once august has registered a Last.fm API account. The
# "secret" in a desktop app isn't truly secret (it ships in the
# binary); Last.fm's docs acknowledge this and the model accepts it.
API_KEY = ""
API_SECRET = ""


def is_configured() -> bool:
    """Whether the api_key/secret are present. The settings page hides
    the Last.fm half until this returns True so users don't see a
    Connect button that can't actually do anything."""
    return bool(API_KEY) and bool(API_SECRET)


# Wire endpoint + the user-facing browser-auth URL.
API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL_TEMPLATE = "https://www.last.fm/api/auth/?api_key={key}&token={token}"

MAX_LISTENS_PER_BATCH = 50

_TIMEOUT_S = 10.0


# ── Signing ────────────────────────────────────────────────────────────────


def _sign(params: Dict[str, str]) -> str:
    """Compute Last.fm's ``api_sig``. Sort params by key (excluding
    ``format`` and ``callback`` per the docs), concatenate
    ``name+value`` pairs, append the shared secret, MD5 the UTF-8
    bytes."""
    items = sorted(
        (k, v) for k, v in params.items() if k not in ("format", "callback") and v is not None
    )
    raw = "".join(f"{k}{v}" for k, v in items) + API_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _signed(params: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of ``params`` with ``api_sig`` added + ``format``
    forced to JSON. Caller should only pass the API params — no need
    to thread ``api_key`` separately, we add it here too."""
    body = dict(params)
    body.setdefault("api_key", API_KEY)
    body["api_sig"] = _sign(body)
    body["format"] = "json"
    return body


def _post(params: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
    """POST to the Last.fm API. Returns ``(ok, body)``. ``ok`` is
    True only on a 2xx + a parseable JSON body that doesn't carry
    Last.fm's ``error`` field."""
    if not is_configured():
        return False, {}
    body = _signed(params)
    try:
        r = requests.post(API_ROOT, data=body, timeout=_TIMEOUT_S)
    except requests.RequestException:
        return False, {}
    try:
        data = r.json()
    except ValueError:
        return False, {}
    if r.status_code < 200 or r.status_code >= 300:
        return False, data
    if isinstance(data, dict) and "error" in data:
        return False, data
    return True, data if isinstance(data, dict) else {}


def _get(params: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
    """GET variant — auth.getToken / auth.getSession use GET in
    Last.fm's docs, though POST also works. Same return shape as
    ``_post``."""
    if not is_configured():
        return False, {}
    body = _signed(params)
    try:
        r = requests.get(API_ROOT, params=body, timeout=_TIMEOUT_S)
    except requests.RequestException:
        return False, {}
    try:
        data = r.json()
    except ValueError:
        return False, {}
    if r.status_code < 200 or r.status_code >= 300:
        return False, data
    if isinstance(data, dict) and "error" in data:
        return False, data
    return True, data if isinstance(data, dict) else {}


# ── Auth flow ──────────────────────────────────────────────────────────────


def get_token() -> Optional[str]:
    """Step 1 of the desktop auth flow: fetch an unauthorized request
    token. Caller then opens ``auth_url(token)`` in the browser."""
    ok, body = _get({"method": "auth.getToken"})
    if not ok:
        return None
    return body.get("token")


def auth_url(token: str) -> str:
    """The page the user opens in their browser to authorize the app.
    They click "Allow", the page confirms, then we can call
    ``get_session(token)`` to exchange for a permanent session key."""
    return AUTH_URL_TEMPLATE.format(key=API_KEY, token=token or "")


def get_session(token: str) -> Optional[Dict[str, str]]:
    """Step 3: poll this until the user has authorized. Returns
    ``{"key": "<session_key>", "name": "<username>"}`` on success,
    ``None`` while still pending or on error.

    The settings dialog should poll on a 2-3 second cadence and give
    the user a Cancel button to stop it. Last.fm errors 14/15 mean
    "still waiting" — both surface as None here so the dialog just
    keeps polling until the user gives up or succeeds."""
    if not token:
        return None
    ok, body = _get({"method": "auth.getSession", "token": token})
    if not ok:
        return None
    sess = body.get("session") if isinstance(body, dict) else None
    if not isinstance(sess, dict):
        return None
    key = sess.get("key")
    name = sess.get("name")
    if not key:
        return None
    return {"key": key, "name": name or ""}


# ── Scrobble writes ────────────────────────────────────────────────────────


def update_now_playing(
    session_key: str, artist: str, track: str, album: str = "", duration_ms: int = 0, mbid: str = ""
) -> bool:
    """Send a ``track.updateNowPlaying`` ping at track start. Returns
    True on success. Failures are silently dropped — now-playing is
    transient, not worth queueing."""
    if not session_key or not artist or not track:
        return False
    params: Dict[str, str] = {
        "method": "track.updateNowPlaying",
        "sk": session_key,
        "artist": artist,
        "track": track,
    }
    if album:
        params["album"] = album
    if duration_ms > 0:
        params["duration"] = str(int(duration_ms // 1000))
    if mbid:
        params["mbid"] = mbid
    ok, _ = _post(params)
    return ok


def scrobble(
    session_key: str,
    artist: str,
    track: str,
    timestamp: int,
    album: str = "",
    duration_ms: int = 0,
    mbid: str = "",
) -> Tuple[bool, Optional[int]]:
    """Submit a single completed scrobble. ``timestamp`` is UNIX
    seconds (UTC) of when the user *started* listening. Returns
    ``(ok, error_code)`` — error_code lets the caller distinguish
    transient (11/16 — retry) from permanent (9 — re-auth, 6 — bad
    params) failures."""
    if not session_key or not artist or not track or timestamp <= 0:
        return False, None
    params: Dict[str, str] = {
        "method": "track.scrobble",
        "sk": session_key,
        "artist[0]": artist,
        "track[0]": track,
        "timestamp[0]": str(int(timestamp)),
    }
    if album:
        params["album[0]"] = album
    if duration_ms > 0:
        params["duration[0]"] = str(int(duration_ms // 1000))
    if mbid:
        params["mbid[0]"] = mbid
    ok, body = _post(params)
    if ok:
        return True, None
    code: Optional[int] = None
    if isinstance(body, dict):
        try:
            code = int(body.get("error"))
        except (TypeError, ValueError):
            code = None
    return False, code


def scrobble_batch(session_key: str, listens: List[Dict[str, Any]]) -> Tuple[bool, Optional[int]]:
    """Bulk-submit up to 50 scrobbles in a single request — used by the
    queue flush path. Each entry needs ``artist`` / ``track`` /
    ``timestamp`` and may optionally carry ``album`` / ``duration_ms`` /
    ``mbid``. Caller is responsible for chunking past 50."""
    if not session_key or not listens:
        return False, None
    if len(listens) > MAX_LISTENS_PER_BATCH:
        listens = listens[:MAX_LISTENS_PER_BATCH]
    params: Dict[str, str] = {
        "method": "track.scrobble",
        "sk": session_key,
    }
    for i, ln in enumerate(listens):
        params[f"artist[{i}]"] = ln.get("artist", "")
        params[f"track[{i}]"] = ln.get("track", "")
        params[f"timestamp[{i}]"] = str(int(ln.get("timestamp", 0)))
        album = ln.get("album", "")
        if album:
            params[f"album[{i}]"] = album
        dur = int(ln.get("duration_ms", 0) or 0)
        if dur > 0:
            params[f"duration[{i}]"] = str(dur // 1000)
        mbid = ln.get("mbid", "")
        if mbid:
            params[f"mbid[{i}]"] = mbid
    ok, body = _post(params)
    if ok:
        return True, None
    code: Optional[int] = None
    if isinstance(body, dict):
        try:
            code = int(body.get("error"))
        except (TypeError, ValueError):
            code = None
    return False, code
