"""Now-playing desktop notifications.

Posts a toast when playback moves to a new track, gated behind
``settings.notify_on_track_change`` (off by default). De-duplicates on
item id so a pause/resume or a position re-emit doesn't re-toast the same
song, and tags the stream so each toast replaces the prior one rather
than stacking. Cross-platform via the notifications facade — the visible
surface is the Windows toast / the Linux notification daemon.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject

logger = logging.getLogger(__name__)

_TAG = "jellytoast-nowplaying"


class NowPlayingNotifier(QObject):
    """Watches PlayerBus and emits a now-playing toast on track change."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_id = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().playback_started.connect(self._on_started)

    def _on_started(self, np):
        try:
            from jellytoast.settings import get_settings

            if not get_settings().notify_on_track_change:
                return

            item_id = getattr(np, "item_id", "") or ""
            # Only toast on a genuine track change — playback_started also
            # fires on resume / re-seed for the same item.
            if item_id and item_id == self._last_id:
                return
            self._last_id = item_id

            title = getattr(np, "title", "") or "Now playing"
            artist = getattr(np, "subtitle", "") or ""
            album = getattr(np, "album", "") or ""
            body = " — ".join(p for p in (artist, album) if p)

            from jellytoast import notifications

            notifications.notify(title, body, tag=_TAG)
        except Exception as e:  # pragma: no cover — best-effort
            logger.debug("now-playing notify failed: %s", e)
