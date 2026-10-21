"""Playlist editing for the Qt Quick front end: add songs / albums /
playlists to a playlist (or a new one), remove songs, rename, delete.

The server calls are the providers' (``add_to_playlist`` /
``create_playlist`` — shared with the desktop's #238 Add to playlist — plus
``remove_from_playlist`` / ``rename_playlist`` / ``delete_playlist``). Every
change drops ``playlist_actions``' cache and emits ``playlists_changed`` so
the Playlists section and open playlist pages refresh, on either UI."""

from __future__ import annotations

import logging
from typing import Dict, List

from PySide6.QtCore import Property, QObject, Signal, Slot

from jellytoast import async_io
from jellytoast import playlist_actions as _pa
from jellytoast.player_state import PlayerBus

logger = logging.getLogger(__name__)


def _provider():
    from jellytoast.providers import get_provider

    return get_provider()


def _track_ids(kind: str, item_id: str) -> List[str]:
    """The song ids a whole item stands for (runs on a worker)."""
    prov = _provider()
    if kind == "track":
        return [item_id]
    if kind == "album":
        return [t.get("Id", "") for t in prov.get_album_tracks(item_id) if t.get("Id")]
    if kind == "playlist":
        return [t.get("Id", "") for t in prov.get_playlist_items(item_id) if t.get("Id")]
    if kind == "artist":
        from jellytoast.quick.actions import _tracks_for

        return [t.get("Id", "") for t in _tracks_for(prov, "artist", item_id) if t.get("Id")]
    return []


class Playlists(QObject):
    changed = Signal()  # the list of playlists / availability
    toast = Signal(str)
    edited = Signal(str)  # playlist id whose contents / name changed ("" = several)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._loading = False
        PlayerBus.get().playlists_changed.connect(self.refresh)
        PlayerBus.get().offline_mode_changed.connect(lambda _on: self.changed.emit())

    # ── state ──
    available = Property(bool, lambda self: _pa.available(), notify=changed)
    loading = Property(bool, lambda self: self._loading, notify=changed)

    def _get_items(self) -> List[Dict]:
        return [
            {
                "id": str(p.get("Id") or ""),
                "name": str(p.get("Name") or ""),
                "count": int(p.get("ChildCount") or 0),
            }
            for p in self._items
        ]

    items = Property("QVariantList", _get_items, notify=changed)

    @Slot()
    def refresh(self) -> None:
        """Fetch the playlists you can edit (cached briefly, like the
        desktop menu)."""
        if not _pa.available():
            self.changed.emit()
            return
        cached = _pa._cached_playlists()
        if cached is not None:
            self._items = cached
            self.changed.emit()
            return
        self._loading = True
        self.changed.emit()
        prov = _provider()

        def _got(items):
            import time

            _pa._cache.update(server=prov.server_url, at=time.monotonic(), items=list(items or []))
            self._items = list(items or [])
            self._loading = False
            self.changed.emit()

        def _failed(e):
            logger.warning("listing playlists failed: %s", e)
            self._loading = False
            self.changed.emit()

        async_io.run_async(prov.get_playlists, on_result=_got, on_error=_failed)

    def _changed(self, playlist_id: str = "") -> None:
        _pa.invalidate_cache()
        PlayerBus.get().playlists_changed.emit()
        self.edited.emit(playlist_id)

    # ── add ──
    @Slot(str, str, str, str)
    def addTo(self, playlist_id: str, playlist_name: str, kind: str, item_id: str) -> None:  # noqa: N802
        """Add a song / album / playlist / artist to ``playlist_id``."""
        prov = _provider()

        def _work() -> int:
            ids = [i for i in _track_ids(kind, item_id) if i]
            if ids:
                prov.add_to_playlist(playlist_id, ids)
            return len(ids)

        def _done(n: int) -> None:
            if not n:
                self.toast.emit(self.tr("Nothing to add"))
                return
            self._changed(playlist_id)
            self.toast.emit(_pa._added_text(n, playlist_name))

        def _failed(e) -> None:
            logger.warning("add to playlist %s failed: %s", playlist_id, e)
            self.toast.emit(self.tr("Couldn't add to {0}").format(playlist_name))

        async_io.run_async(_work, on_result=_done, on_error=_failed)

    @Slot(str, str, str)
    def createWith(self, name: str, kind: str, item_id: str) -> None:  # noqa: N802
        """A new playlist called ``name`` holding the item (``kind`` "" =
        empty)."""
        name = (name or "").strip()
        if not name:
            return
        prov = _provider()

        def _work() -> int:
            ids = [i for i in _track_ids(kind, item_id) if i] if kind else []
            prov.create_playlist(name, ids)
            return len(ids)

        def _done(n: int) -> None:
            self._changed()
            self.toast.emit(
                self.tr("Created {0}").format(name) if n == 0 else _pa._added_text(n, name)
            )

        def _failed(e) -> None:
            logger.warning("create playlist %r failed: %s", name, e)
            self.toast.emit(self.tr("Couldn't create {0}").format(name))

        async_io.run_async(_work, on_result=_done, on_error=_failed)

    # ── edit ──
    @Slot(QObject, int)
    def removeRow(self, tracks, row: int) -> None:  # noqa: N802
        """Remove one song from the playlist a TracksModel is showing — gone
        from the list at once, then from the server."""
        entry = tracks.take_row(row)
        if entry is not None:
            self.remove_entries(tracks.album_id, [entry])

    def remove_entries(self, playlist_id: str, entries: List[tuple]) -> None:
        """Remove ``(position, item)`` entries (from a TracksModel)."""
        prov = _provider()

        def _done(_r=None) -> None:
            self._changed(playlist_id)
            n = len(entries)
            self.toast.emit(
                self.tr("Removed from playlist")
                if n == 1
                else self.tr("Removed {0} songs from playlist").format(n)
            )

        def _failed(e) -> None:
            logger.warning("remove from playlist %s failed: %s", playlist_id, e)
            self.toast.emit(self.tr("Couldn't remove from playlist"))
            self.edited.emit(playlist_id)  # reload: show what's really there

        async_io.run_async(
            prov.remove_from_playlist, playlist_id, entries, on_result=_done, on_error=_failed
        )

    @Slot(str, str)
    def rename(self, playlist_id: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        prov = _provider()
        async_io.run_async(
            prov.rename_playlist,
            playlist_id,
            name,
            on_result=lambda _r: (self._changed(playlist_id), self.toast.emit(self.tr("Renamed"))),
            on_error=lambda e: (
                logger.warning("rename playlist %s failed: %s", playlist_id, e),
                self.toast.emit(self.tr("Couldn't rename the playlist")),
            ),
        )

    @Slot(str, str)
    def remove(self, playlist_id: str, name: str) -> None:
        """Delete a playlist (its songs stay in the library)."""
        prov = _provider()
        async_io.run_async(
            prov.delete_playlist,
            playlist_id,
            on_result=lambda _r: (
                self._changed(playlist_id),
                self.toast.emit(self.tr("Deleted {0}").format(name)),
            ),
            on_error=lambda e: (
                logger.warning("delete playlist %s failed: %s", playlist_id, e),
                self.toast.emit(self.tr("Couldn't delete {0}").format(name)),
            ),
        )
