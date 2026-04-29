"""
Playback queue manager.
Handles queue mutations, current-track tracking, shuffle, repeat, and persistence.
"""

import random
from typing import List, Dict, Optional, Any
from PyQt6.QtCore import QObject, pyqtSlot
from modules.player_state import PlayerBus, RepeatMode, NowPlaying, set_now_playing
from modules.settings import get_settings
from modules.jellyfin_api import get_api


class QueueManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_api()

        self._queue: List[Dict[str, Any]] = []
        self._original_order: List[Dict[str, Any]] = []  # for un-shuffling
        self._index: int = -1
        self._repeat: RepeatMode = RepeatMode(self.settings.repeat_mode)
        self._shuffle: bool = self.settings.shuffle

        self._connect()

        # Restore previous queue
        saved_queue, saved_index = self.settings.load_queue()
        if saved_queue:
            self._queue = saved_queue
            self._original_order = list(saved_queue)
            self._index = saved_index if 0 <= saved_index < len(saved_queue) else -1
            self.bus.queue_changed.emit(self._queue, self._index)

    def _connect(self):
        self.bus.queue_play_now.connect(self.play_now)
        self.bus.queue_add_next.connect(self.add_next)
        self.bus.queue_add_end.connect(self.add_to_end)
        self.bus.queue_clear.connect(self.clear)
        self.bus.next_track.connect(self.next)
        self.bus.prev_track.connect(self.previous)
        self.bus.track_jumped.connect(self.jump_to)
        self.bus.repeat_changed.connect(self._on_repeat_changed)
        self.bus.shuffle_changed.connect(self._on_shuffle_changed)
        self.bus.playback_ended.connect(self._on_playback_ended)

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def queue(self) -> List[Dict]:
        return list(self._queue)

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def current_item(self) -> Optional[Dict]:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    @property
    def has_next(self) -> bool:
        if self._repeat == RepeatMode.ALL or self._repeat == RepeatMode.ONE:
            return len(self._queue) > 0
        return self._index < len(self._queue) - 1

    @property
    def has_previous(self) -> bool:
        if self._repeat == RepeatMode.ALL:
            return len(self._queue) > 0
        return self._index > 0

    @property
    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    @property
    def shuffle_enabled(self) -> bool:
        return self._shuffle

    # ── Mutations ───────────────────────────────────────────────────────────

    @pyqtSlot(list, int)
    def play_now(self, items: List[Dict], start_index: int = 0):
        """Replace the queue and start playing from start_index."""
        if not items:
            return
        self._queue = list(items)
        self._original_order = list(items)
        if self._shuffle:
            self._apply_shuffle(keep_at_top=start_index)
            start_index = 0
        self._index = max(0, min(start_index, len(self._queue) - 1))
        self.bus.queue_changed.emit(self._queue, self._index)
        self._play_current()

    @pyqtSlot(list)
    def add_next(self, items: List[Dict]):
        if not items:
            return
        if not self._queue:
            self.play_now(items, 0)
            return
        insert_at = self._index + 1
        for i, item in enumerate(items):
            self._queue.insert(insert_at + i, item)
        self._original_order = list(self._queue)
        self.bus.queue_changed.emit(self._queue, self._index)
        self._save()

    @pyqtSlot(list)
    def add_to_end(self, items: List[Dict]):
        if not items:
            return
        was_empty = len(self._queue) == 0
        self._queue.extend(items)
        self._original_order = list(self._queue)
        if was_empty:
            self._index = 0
            self.bus.queue_changed.emit(self._queue, self._index)
            self._play_current()
        else:
            self.bus.queue_changed.emit(self._queue, self._index)
        self._save()

    @pyqtSlot()
    def clear(self):
        self._queue = []
        self._original_order = []
        self._index = -1
        self.bus.queue_changed.emit(self._queue, self._index)
        self._save()

    def remove_at(self, index: int):
        if 0 <= index < len(self._queue):
            self._queue.pop(index)
            if index < self._index:
                self._index -= 1
            elif index == self._index:
                # Removed current track — play next, or stop
                if self._index >= len(self._queue):
                    self._index = -1
                    self.bus.stop_requested.emit()
                else:
                    self._play_current()
            self._original_order = list(self._queue)
            self.bus.queue_changed.emit(self._queue, self._index)
            self._save()

    # ── Navigation ──────────────────────────────────────────────────────────

    @pyqtSlot()
    def next(self):
        if not self._queue:
            return
        if self._repeat == RepeatMode.ONE:
            self._play_current()
            return
        if self._index < len(self._queue) - 1:
            self._index += 1
            self._play_current()
        elif self._repeat == RepeatMode.ALL:
            self._index = 0
            self._play_current()
        else:
            self.bus.stop_requested.emit()

    @pyqtSlot()
    def previous(self):
        if not self._queue:
            return
        # If >3s into track, restart it; else go back
        from modules.player_state import get_now_playing
        np = get_now_playing()
        if np.position > 3000:
            self.bus.seek_requested.emit(0)
            return
        if self._index > 0:
            self._index -= 1
            self._play_current()
        elif self._repeat == RepeatMode.ALL:
            self._index = len(self._queue) - 1
            self._play_current()
        else:
            self.bus.seek_requested.emit(0)

    @pyqtSlot(int)
    def jump_to(self, index: int):
        if 0 <= index < len(self._queue):
            self._index = index
            self._play_current()

    # ── Repeat / Shuffle ────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_repeat_changed(self, mode: str):
        self._repeat = RepeatMode(mode)
        self.settings.repeat_mode = mode

    @pyqtSlot(bool)
    def _on_shuffle_changed(self, enabled: bool):
        self._shuffle = enabled
        self.settings.shuffle = enabled
        if enabled:
            self._apply_shuffle(keep_at_top=self._index)
            self._index = 0
        else:
            # Restore original order, keeping current item the current
            current = self.current_item
            self._queue = list(self._original_order)
            if current:
                try:
                    self._index = next(i for i, it in enumerate(self._queue)
                                        if it.get("Id") == current.get("Id"))
                except StopIteration:
                    self._index = 0
        self.bus.queue_changed.emit(self._queue, self._index)
        self._save()

    def _apply_shuffle(self, keep_at_top: int = -1):
        if not self._queue:
            return
        if 0 <= keep_at_top < len(self._queue):
            head = self._queue[keep_at_top]
            rest = [it for i, it in enumerate(self._queue) if i != keep_at_top]
            random.shuffle(rest)
            self._queue = [head] + rest
        else:
            random.shuffle(self._queue)

    # ── Internals ───────────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_playback_ended(self):
        self.next()

    def _play_current(self):
        item = self.current_item
        if not item:
            return
        np = self._build_now_playing(item)
        set_now_playing(np)
        self.bus.queue_changed.emit(self._queue, self._index)
        self.bus.play_requested.emit(np)
        self._save()

    def _build_now_playing(self, item: Dict) -> NowPlaying:
        item_id = item.get("Id", "")
        item_type = item.get("Type", "")

        # Determine stream URL based on item type
        if item_type == "Audio":
            stream_url = self.api.get_audio_stream_url(item_id)
        else:
            stream_url = self.api.get_video_stream_url(item_id)

        # Image: prefer album art for audio, primary for video
        image_id = item.get("AlbumId") if item_type == "Audio" and item.get("AlbumId") else item_id
        thumb_url = self.api.get_image_url(image_id, "Primary", 600)

        # Subtitle: artist for audio, series for episode
        if item_type == "Audio":
            artists = item.get("Artists", [])
            subtitle = ", ".join(artists) if artists else item.get("AlbumArtist", "")
        elif item_type == "Episode":
            subtitle = item.get("SeriesName", "")
        else:
            subtitle = str(item.get("ProductionYear", ""))

        return NowPlaying(
            item_id=item_id,
            title=item.get("Name", "Unknown"),
            subtitle=subtitle,
            album=item.get("Album", ""),
            year=str(item.get("ProductionYear", "")),
            duration=item.get("RunTimeTicks", 0) // 10_000,
            stream_url=stream_url,
            thumb_url=thumb_url,
            item_type=item_type,
            is_favorite=item.get("UserData", {}).get("IsFavorite", False),
            raw=item,
        )

    def _save(self):
        self.settings.save_queue(self._queue, self._index)
