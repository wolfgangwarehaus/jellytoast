"""Whole-item actions for the Qt Quick front end (long-press menus on
albums / playlists / artists): play, shuffle, play next, add to queue.

Fetches the item's tracks off the GUI thread, then hands them to the queue
through the same ``PlayerBus`` signals the Widgets UI uses."""

from __future__ import annotations

import logging
import random
from typing import Dict, List

from PySide6.QtCore import QObject, Signal, Slot

from jellytoast import async_io
from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

logger = logging.getLogger(__name__)

_KIND = {"album": QueueKind.ALBUM, "playlist": QueueKind.PLAYLIST, "artist": QueueKind.ARTIST}


def _tracks_for(prov, kind: str, item_id: str) -> List[Dict]:
    if kind == "album":
        return list(prov.get_album_tracks(item_id) or [])
    if kind == "playlist":
        return list(prov.get_playlist_items(item_id) or [])
    if kind == "artist":
        albums = list(prov.get_artist_albums(item_id) or [])
        albums.sort(key=lambda a: int(a.get("ProductionYear") or 0))
        out: List[Dict] = []
        for a in albums:
            out.extend(prov.get_album_tracks(a.get("Id", "")) or [])
        return out
    raise ValueError(f"no tracks for kind {kind!r}")


class Actions(QObject):
    toast = Signal(str)  # short confirmation for the UI ("Added to queue")

    @Slot(str, str, str, str)
    def run(self, kind: str, item_id: str, name: str, mode: str) -> None:
        """``mode``: ``play`` | ``shuffle`` | ``next`` | ``end``."""
        from jellytoast.providers import get_provider

        prov = get_provider()

        def _go(tracks: List[Dict]) -> None:
            if not tracks:
                self.toast.emit(self.tr("Nothing to play"))
                return
            bus = PlayerBus.get()
            if mode in ("play", "shuffle"):
                ctx = QueueContext(
                    kind=_KIND.get(kind, QueueKind.MANUAL), source_id=item_id, source_label=name
                )
                bus.shuffle_changed.emit(mode == "shuffle")
                start = random.randrange(len(tracks)) if mode == "shuffle" else 0
                bus.queue_play_now.emit(tracks, start, ctx)
            elif mode == "next":
                bus.queue_add_next.emit(tracks)
                self.toast.emit(self.tr("Playing next"))
            else:
                bus.queue_add_end.emit(tracks)
                self.toast.emit(self.tr("Added to queue"))

        async_io.run_async(
            _tracks_for,
            prov,
            kind,
            item_id,
            on_result=_go,
            on_error=lambda e: logger.warning("%s %s %s failed: %s", mode, kind, item_id, e),
        )

    @Slot(str, str, str)
    def startRadio(self, kind: str, item_id: str, name: str) -> None:  # noqa: N802
        """Endless radio seeded from an album / artist / genre (same as the
        Widgets "Start radio")."""
        from jellytoast.ui_helpers import start_seed_radio

        start_seed_radio(kind, item_id, name)
        self.toast.emit(self.tr("Starting radio…"))

    @Slot(str, bool)
    def setFavorite(self, item_id: str, on: bool) -> None:  # noqa: N802
        from jellytoast.ui_helpers import toggle_favorite_async

        toggle_favorite_async(item_id, on)
        PlayerBus.get().favorite_toggled.emit(item_id, on)
        self.toast.emit(self.tr("Added to favorites") if on else self.tr("Removed from favorites"))
