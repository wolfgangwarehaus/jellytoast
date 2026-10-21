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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = PlayerBus.get()
        self._playing = False
        self._position = 0
        self._duration = 0
        b = self._bus
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

    # ── properties ──
    def _title(self) -> str:
        return get_now_playing().title or ""

    def _artist(self) -> str:
        return get_now_playing().subtitle or ""

    def _cover(self) -> str:
        return get_now_playing().thumb_url or ""

    def _has_track(self) -> bool:
        return bool(get_now_playing().item_id)

    title = Property(str, _title, notify=changed)
    artist = Property(str, _artist, notify=changed)
    cover = Property(str, _cover, notify=changed)
    hasTrack = Property(bool, _has_track, notify=changed)
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
