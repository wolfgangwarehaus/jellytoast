"""Scrobbling — client-side Last.fm + ListenBrainz, unified across
Jellyfin and Subsonic. See docs/scrobbling.md for the full design.

Public API:

- ``get_scrobble_manager()`` — process-wide ``ScrobbleManager`` accessor.
  Lazy-constructs on first call so importing this package from settings
  paths doesn't drag in the bus / network plumbing before the GUI is up.

The package is internally split into:

- ``manager`` — the ``QObject`` wired to ``PlayerBus``: the 30s/50%/4min
  eligibility rule, fan-out to enabled services, queue handoff.
- ``listenbrainz`` — pasted-token submit-listens client.
- ``lastfm`` — desktop browser-auth + signed POSTs (deferred — needs the
  api_key/api_secret to be filled in).
- ``queue`` — JSON-backed pending-scrobble store, flushed on reconnect.
"""

from typing import Callable, Optional

from .manager import ScrobbleManager

_singleton: Optional["ScrobbleManager"] = None


def refresh_server_scrobble_flags(on_done: Optional[Callable] = None) -> None:
    """Detect whether a *second* scrobbler (the music server, e.g. Navidrome)
    is already submitting this account's ListenBrainz listens, and persist the
    result to ``settings.server_scrobbles_listenbrainz`` — the double-scrobble
    guard the manager gates on. Best-effort + async (off the GUI thread).

    The signal is ListenBrainz's own ``submission_client`` field: jellytoast
    stamps ``"jellytoast"``, the server stamps its own name. See
    ``modules/scrobble/server_scrobble_detect.py`` for why this replaced the
    (unworkable) Navidrome native-API probe. No-op when LB isn't configured.

    ``on_done(Result)`` — optional callback on the GUI thread after the flag
    is written (the settings UI uses it to refresh its banner).
    """
    from modules.async_io import run_async
    from modules.settings import get_settings

    from .server_scrobble_detect import Result, detect

    s = get_settings()
    if not s.listenbrainz_enabled or not s.listenbrainz_username:
        # Nothing to detect against — clear the "checked" flag so the UI
        # shows the neutral state, and leave server_scrobbles_listenbrainz as
        # last known (a deliberate sign-out clears it elsewhere).
        if on_done is not None:
            on_done(Result())
        return

    def _apply(res):
        if res.checked:
            s.server_scrobbles_listenbrainz = res.server_scrobbles
            s.server_scrobble_check_done = True
            s.flush()
            # Refresh the now-playing "Scrobble" badge (+ any other listener).
            try:
                from modules.player_state import PlayerBus

                PlayerBus.get().scrobble_status_changed.emit()
            except Exception:
                pass
        if on_done is not None:
            on_done(res)

    run_async(
        detect,
        s.listenbrainz_url,
        s.listenbrainz_username,
        s.listenbrainz_token,
        on_result=_apply,
        on_error=lambda _e: on_done(Result()) if on_done is not None else None,
    )


def get_scrobble_manager() -> "ScrobbleManager":
    """Return the process-wide ScrobbleManager. Constructs on first
    call. Safe to call from any module that already has a QApplication
    and the PlayerBus singleton up — i.e. anywhere after the main
    window has started building."""
    global _singleton
    if _singleton is None:
        _singleton = ScrobbleManager()
    return _singleton


__all__ = [
    "ScrobbleManager",
    "get_scrobble_manager",
    "refresh_server_scrobble_flags",
]
