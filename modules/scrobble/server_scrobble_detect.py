"""Detect a *second* ListenBrainz scrobbler on the user's account.

Navidrome's API does not expose whether the user has linked ListenBrainz
(confirmed: the native ``/api/user/{id}`` record carries no scrobble field,
and the Subsonic API has nothing either), so we cannot read the server's
config. But ListenBrainz *itself* records the ``submission_client`` of every
listen — jellytoast stamps ``"jellytoast"`` (see
``modules/scrobble/listenbrainz.py``), Navidrome stamps ``"navidrome"``. So if
the user's recent LB listens carry a ``submission_client`` OTHER than
``jellytoast``, a second scrobbler (the music server, or another app on the
same LB account) is active and jellytoast's in-app scrobbling would
double-submit.

This replaces the old native-API probe (``navidrome_detect``), which could
never work: even with the correct ``X-ND-Authorization`` header the user
record exposes no LB/Last.fm linkage.

Read-only — one GET against the LB ``listens`` endpoint. Best-effort: returns
``Result(checked=False)`` on any error so the caller leaves state untouched
(no false "server is scrobbling" from a network blip).
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from modules.version import __version__

# The submission_client jellytoast stamps on its own listens — must match
# modules/scrobble/listenbrainz.py. Anything else in the user's listens is a
# DIFFERENT scrobbler.
OUR_CLIENT = "jellytoast"
_DEFAULT_URL = "https://api.listenbrainz.org"
_TIMEOUT_S = 8.0


@dataclass
class Result:
    """Outcome of the LB submission-client probe.

    ``checked`` is True only when we successfully read the listens list (even
    an empty one). ``foreign_clients`` is the de-duplicated, lowercased set of
    non-jellytoast submission_clients seen — non-empty means a second
    scrobbler is active. When ``checked`` is False the caller should treat it
    as "couldn't tell" and not act on ``foreign_clients``.
    """

    checked: bool = False
    foreign_clients: tuple[str, ...] = ()

    @property
    def server_scrobbles(self) -> bool:
        """True when at least one non-jellytoast client submitted recent
        listens — i.e. another scrobbler (the server) is covering this
        account."""
        return bool(self.foreign_clients)


def server_scrobbler_name(server_is_navidrome: bool) -> str:
    """The LB ``submission_client`` the CURRENT server stamps on listens it
    forwards — used to decide whether to defer in-app scrobbling.

    We suppress in-app LB scrobbling only when THIS server's own scrobbler is
    the foreign submitter, so a stale name from a previously-used server (e.g.
    ``navidrome`` still lingering in recent listens after a swap to Jellyfin)
    does NOT keep suppressing. Returns ``""`` when we don't have a reliable
    name — notably any non-Navidrome server: vanilla Jellyfin doesn't forward
    to ListenBrainz at all, so ``""`` → never auto-suppress → in-app
    scrobbling stays on (a Jellyfin LB-plugin user can still pause it by hand
    / via the override, and the plugin's client name can be added here later).
    """
    return "navidrome" if server_is_navidrome else ""


def detect(
    lb_url: str,
    username: str,
    token: str = "",
    *,
    count: int = 30,
    timeout: float = _TIMEOUT_S,
) -> Result:
    """Best-effort: read the user's recent ListenBrainz listens and report
    which non-jellytoast clients submitted them. Synchronous + blocking — the
    caller dispatches via ``modules.async_io.run_async``.

    Returns ``Result(checked=False)`` on any error path (no username, network,
    non-200, bad JSON). An empty account (``checked=True``, no foreign
    clients) reads as "no second scrobbler seen (yet)".
    """
    if not username:
        return Result()
    base = (lb_url or _DEFAULT_URL).rstrip("/")
    headers = {"User-Agent": f"jellytoast/{__version__}"}
    if token:
        # The listens endpoint is public for public profiles, but the token
        # covers private profiles + avoids rate caps.
        headers["Authorization"] = f"Token {token}"
    try:
        r = requests.get(
            f"{base}/1/user/{username}/listens",
            params={"count": int(count)},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException:
        return Result()
    if r.status_code != 200:
        return Result()
    try:
        listens = ((r.json() or {}).get("payload") or {}).get("listens") or []
    except ValueError:
        return Result()

    foreign: list[str] = []
    seen: set[str] = set()
    for ln in listens:
        if not isinstance(ln, dict):
            continue
        ai = (ln.get("track_metadata") or {}).get("additional_info") or {}
        client = (ai.get("submission_client") or "").strip().lower()
        if client and client != OUR_CLIENT and client not in seen:
            seen.add(client)
            foreign.append(client)
    return Result(checked=True, foreign_clients=tuple(foreign))
