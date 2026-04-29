"""
Player state: NowPlaying dataclass + Qt signal bus + repeat/shuffle modes.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from PyQt6.QtCore import QObject, pyqtSignal


class RepeatMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


@dataclass
class NowPlaying:
    item_id: str = ""
    title: str = ""
    subtitle: str = ""        # artist / series
    album: str = ""
    year: str = ""
    duration: int = 0         # ms
    position: int = 0         # ms
    is_paused: bool = False
    thumb_url: str = ""
    stream_url: str = ""
    item_type: str = ""       # Movie | Episode | Audio
    is_favorite: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_audio(self) -> bool:
        return self.item_type == "Audio"

    @property
    def is_video(self) -> bool:
        return self.item_type in ("Movie", "Episode")

    @property
    def position_ticks(self) -> int:
        return self.position * 10_000

    @property
    def duration_ticks(self) -> int:
        return self.duration * 10_000


class PlayerBus(QObject):
    """Single source of truth for cross-component playback events."""

    # ── Playback control (UI → backend) ─────────────────────────────────────
    play_requested = pyqtSignal(object)        # NowPlaying
    pause_toggled = pyqtSignal()
    stop_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)           # ms (absolute)
    seek_relative = pyqtSignal(int)            # ms (delta)
    volume_changed = pyqtSignal(int)           # 0–100
    mute_toggled = pyqtSignal()

    # ── Queue control ────────────────────────────────────────────────────────
    queue_play_now = pyqtSignal(list, int)     # items, start_index
    queue_add_next = pyqtSignal(list)          # items
    queue_add_end = pyqtSignal(list)           # items
    queue_clear = pyqtSignal()
    next_track = pyqtSignal()
    prev_track = pyqtSignal()
    queue_changed = pyqtSignal(list, int)      # full_queue, current_index
    track_jumped = pyqtSignal(int)             # index in queue

    repeat_changed = pyqtSignal(str)
    shuffle_changed = pyqtSignal(bool)

    # ── State updates (backend → UI) ────────────────────────────────────────
    position_updated = pyqtSignal(int)         # ms
    duration_set = pyqtSignal(int)             # ms
    playback_started = pyqtSignal(object)      # NowPlaying
    playback_paused = pyqtSignal()
    playback_resumed = pyqtSignal()
    playback_stopped = pyqtSignal()
    playback_ended = pyqtSignal()
    volume_state = pyqtSignal(int)
    mute_state = pyqtSignal(bool)

    # ── Cast ────────────────────────────────────────────────────────────────
    cast_started = pyqtSignal(str)
    cast_stopped = pyqtSignal()
    cast_devices_updated = pyqtSignal(list)

    # ── Favorite ────────────────────────────────────────────────────────────
    favorite_toggled = pyqtSignal(str, bool)   # item_id, is_favorite

    # ── UI ──────────────────────────────────────────────────────────────────
    open_main_window = pyqtSignal()
    show_mini_player = pyqtSignal()
    hide_mini_player = pyqtSignal()
    navigate_to_item = pyqtSignal(dict)        # item dict
    show_now_playing = pyqtSignal()
    notify_track = pyqtSignal(object)          # NowPlaying

    _instance: Optional["PlayerBus"] = None

    @classmethod
    def get(cls) -> "PlayerBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


# ── Now-playing global state ────────────────────────────────────────────────

_now_playing = NowPlaying()


def get_now_playing() -> NowPlaying:
    return _now_playing


def set_now_playing(item: NowPlaying):
    global _now_playing
    _now_playing = item
