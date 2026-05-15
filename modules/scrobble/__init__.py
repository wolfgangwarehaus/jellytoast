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

from typing import Optional

from .manager import ScrobbleManager

_singleton: Optional["ScrobbleManager"] = None


def get_scrobble_manager() -> "ScrobbleManager":
    """Return the process-wide ScrobbleManager. Constructs on first
    call. Safe to call from any module that already has a QApplication
    and the PlayerBus singleton up — i.e. anywhere after the main
    window has started building."""
    global _singleton
    if _singleton is None:
        _singleton = ScrobbleManager()
    return _singleton


__all__ = ["ScrobbleManager", "get_scrobble_manager"]
