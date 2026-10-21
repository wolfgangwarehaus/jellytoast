"""Now-playing state + transport for the Qt Quick front end.

Mirrors ``PlayerBus`` into properties QML binds to, and turns QML taps into
the same bus signals the Widgets UI emits — one player, two front ends.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QObject, Signal, Slot

from jellytoast import async_io
from jellytoast.player_state import PlayerBus, QueueContext, QueueKind, get_now_playing

logger = logging.getLogger(__name__)


class PlayerController(QObject):
    changed = Signal()  # title / artist / cover / playing
    positionChanged = Signal()

    modesChanged = Signal()  # repeat / shuffle
    contextChanged = Signal()

    def __init__(self, parent=None, queue=None):
        super().__init__(parent)
        self._bus = PlayerBus.get()
        self._playing = False
        self._position = 0
        self._duration = 0
        self._repeat = str(getattr(getattr(queue, "repeat_mode", None), "value", "off") or "off")
        self._shuffle = bool(getattr(queue, "shuffle_enabled", False))
        ctx = getattr(queue, "context", None)
        self._source = getattr(ctx, "source_label", "") or ""
        b = self._bus
        b.repeat_changed.connect(self._on_repeat)
        b.shuffle_changed.connect(self._on_shuffle)
        b.queue_context_changed.connect(self._on_context)
        b.favorite_toggled.connect(self._on_favorite)
        b.playback_started.connect(self._on_started)
        b.playback_restored.connect(self._on_restored)
        b.playback_paused.connect(lambda: self._set_playing(False))
        b.playback_resumed.connect(lambda: self._set_playing(True))
        b.playback_stopped.connect(self._on_stopped)
        b.position_updated.connect(self._on_position)
        b.duration_set.connect(self._on_duration)

    # ── bus → properties ──
    def _on_started(self, np) -> None:
        self._duration = int(getattr(np, "duration", 0) or 0)
        self._set_playing(True)

    def _on_restored(self, np) -> None:
        self._duration = int(getattr(np, "duration", 0) or 0)
        self._position = int(getattr(np, "position", 0) or 0)
        self._set_playing(False)
        self.positionChanged.emit()

    def _on_stopped(self) -> None:
        self._position = 0
        self._set_playing(False)
        self.positionChanged.emit()

    def _set_playing(self, on: bool) -> None:
        self._playing = on
        self.changed.emit()

    def _on_position(self, ms: int) -> None:
        self._position = int(ms)
        self.positionChanged.emit()

    def _on_duration(self, ms: int) -> None:
        self._duration = int(ms)
        self.positionChanged.emit()

    def _on_repeat(self, mode: str) -> None:
        self._repeat = mode
        self.modesChanged.emit()

    def _on_shuffle(self, on: bool) -> None:
        self._shuffle = bool(on)
        self.modesChanged.emit()

    def _on_context(self, ctx) -> None:
        self._source = getattr(ctx, "source_label", "") or ""
        self.contextChanged.emit()

    def _on_favorite(self, item_id: str, _fav: bool) -> None:
        if item_id == get_now_playing().item_id:
            self.changed.emit()

    # ── properties ──
    def _title(self) -> str:
        return get_now_playing().title or ""

    def _artist(self) -> str:
        return get_now_playing().subtitle or ""

    def _cover(self) -> str:
        return get_now_playing().thumb_url or ""

    def _big_cover(self) -> str:
        np = get_now_playing()
        image_id = np.image_id or np.item_id
        if not image_id:
            return ""
        from jellytoast.providers import get_provider

        try:
            return get_provider().get_image_url(image_id, "Primary", 1024) or np.thumb_url or ""
        except Exception:
            return np.thumb_url or ""

    def _album_id(self) -> str:
        raw = get_now_playing().raw or {}
        return str(raw.get("AlbumId") or "")

    def _artist_id(self) -> str:
        raw = get_now_playing().raw or {}
        for key in ("AlbumArtists", "ArtistItems"):
            for a in raw.get(key) or []:
                if isinstance(a, dict) and a.get("Id"):
                    return str(a["Id"])
        return ""

    def _has_track(self) -> bool:
        return bool(get_now_playing().item_id)

    title = Property(str, _title, notify=changed)
    artist = Property(str, _artist, notify=changed)
    cover = Property(str, _cover, notify=changed)
    hasTrack = Property(bool, _has_track, notify=changed)
    itemId = Property(str, lambda self: get_now_playing().item_id or "", notify=changed)
    album = Property(str, lambda self: get_now_playing().album or "", notify=changed)
    albumId = Property(str, _album_id, notify=changed)
    artistId = Property(str, _artist_id, notify=changed)
    bigCover = Property(str, _big_cover, notify=changed)
    favorite = Property(bool, lambda self: bool(get_now_playing().is_favorite), notify=changed)
    repeat = Property(str, lambda self: self._repeat, notify=modesChanged)
    shuffle = Property(bool, lambda self: self._shuffle, notify=modesChanged)
    source = Property(str, lambda self: self._source, notify=contextChanged)
    playing = Property(bool, lambda self: self._playing, notify=changed)
    position = Property(int, lambda self: self._position, notify=positionChanged)
    duration = Property(int, lambda self: self._duration, notify=positionChanged)

    # ── transport ──
    @Slot()
    def playPause(self) -> None:  # noqa: N802 — QML-facing name
        self._bus.pause_toggled.emit()

    @Slot()
    def next(self) -> None:
        self._bus.next_track.emit()

    @Slot()
    def previous(self) -> None:
        self._bus.prev_track.emit()

    @Slot(int)
    def seek(self, ms: int) -> None:
        self._position = int(ms)
        self.positionChanged.emit()
        self._bus.seek_requested.emit(int(ms))

    @Slot()
    def toggleFavorite(self) -> None:  # noqa: N802
        np = get_now_playing()
        if not np.item_id:
            return
        from jellytoast.ui_helpers import toggle_favorite_async

        new_state = not np.is_favorite
        toggle_favorite_async(np.item_id, new_state)
        np.is_favorite = new_state
        self._bus.favorite_toggled.emit(np.item_id, new_state)

    @Slot()
    def cycleRepeat(self) -> None:  # noqa: N802
        order = ["off", "all", "one"]
        nxt = order[(order.index(self._repeat) + 1) % 3] if self._repeat in order else "all"
        self._bus.repeat_changed.emit(nxt)

    @Slot()
    def toggleShuffle(self) -> None:  # noqa: N802
        self._bus.shuffle_changed.emit(not self._shuffle)

    @Slot(str, str)
    def playAlbum(self, album_id: str, name: str) -> None:  # noqa: N802
        """Fetch the album's tracks (off the GUI thread) and play from the top."""
        from jellytoast.providers import get_provider

        prov = get_provider()

        def _go(tracks):
            if not tracks:
                return
            ctx = QueueContext(kind=QueueKind.ALBUM, source_id=album_id, source_label=name)
            self._bus.queue_play_now.emit(list(tracks), 0, ctx)

        async_io.run_async(
            prov.get_album_tracks,
            album_id,
            on_result=_go,
            on_error=lambda e: logger.warning("play album %s failed: %s", album_id, e),
        )
