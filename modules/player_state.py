"""
Player state: NowPlaying dataclass + Qt signal bus + repeat/shuffle modes.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from PySide6.QtCore import QObject, Signal


class RepeatMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


class QueueKind(str, Enum):
    """Source of the active queue. Drives the right-pane rendering on the
    now-playing page (album track listing vs flat queue view), distinguishes
    library-shuffle from album-shuffle, and stamps MPRIS metadata.

    Modeled after Music Assistant's `PlayerQueue` envelope and Strawberry's
    `Playlist::special_type_`. Adding a new kind: pick a label that survives
    when the user serializes their session, and update the queue page
    pane-picker that switches on this enum.
    """
    ALBUM = "album"
    PLAYLIST = "playlist"
    ARTIST = "artist"
    SHUFFLE = "shuffle"          # library-wide random
    SEARCH = "search"
    MANUAL = "manual"            # user-built ad-hoc queue
    INSTANT_MIX = "instant_mix"  # Jellyfin-radio-style auto-extension


@dataclass
class QueueContext:
    """Where the active queue came from. Immutable for the lifetime of one
    queue install — replacing the queue (e.g. clicking a different album)
    creates a new QueueContext, never mutates the existing one."""
    kind: QueueKind = QueueKind.MANUAL
    source_id: str = ""    # AlbumId / PlaylistId / etc., empty for shuffle/manual
    source_label: str = ""  # human-readable name for the right pane header
    source_icon: str = ""  # cover-art URL (album/playlist art), or empty


@dataclass
class Queue:
    """Per Strawberry's `Playlist`: keep `original_items` immutable for the
    lifetime of this queue and mutate only `play_order` (a permutation of
    indices into `original_items`). That lets the queue page render the
    "album order" right pane without losing it when shuffle is on, since
    the displayed list doesn't move — only the play head's walk through it.

    `manual_overlay` is reserved for Strawberry-style "Play next" inserts
    that should ride on top of any context (album/playlist) without
    converting it to a manual queue. Currently unused — the queue UI will
    wire it up; `next()` will drain this list FIFO before stepping
    `play_order`. Until then it stays empty.
    """
    context: QueueContext = field(default_factory=QueueContext)
    original_items: List[Dict[str, Any]] = field(default_factory=list)
    play_order: List[int] = field(default_factory=list)
    current_index: int = -1     # index INTO play_order, not original_items
    manual_overlay: List[Dict[str, Any]] = field(default_factory=list)
    # True once the queue has diverged from its source context (user
    # added a track, dragged a row, or removed an item). The
    # now-playing page's right-pane kicker shows "QUEUE" instead of
    # "ALBUM" / "PLAYLIST" / etc. when this is set, so the user knows
    # the list isn't a faithful copy of the source anymore. Reset to
    # False whenever ``play_now`` installs a fresh queue.
    is_modified: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.original_items

    @property
    def length(self) -> int:
        return len(self.play_order)

    @property
    def current_item(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.current_index < len(self.play_order):
            return self.original_items[self.play_order[self.current_index]]
        return None

    def play_ordered(self) -> List[Dict[str, Any]]:
        """Items in playback order — what MPRIS, the mini player, and the
        flat-queue right pane all want to render."""
        return [self.original_items[i] for i in self.play_order
                if 0 <= i < len(self.original_items)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": {
                "kind": self.context.kind.value,
                "source_id": self.context.source_id,
                "source_label": self.context.source_label,
                "source_icon": self.context.source_icon,
            },
            "original_items": self.original_items,
            "play_order": self.play_order,
            "current_index": self.current_index,
            "manual_overlay": self.manual_overlay,
            "is_modified": self.is_modified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Queue":
        ctx_raw = data.get("context") or {}
        try:
            kind = QueueKind(ctx_raw.get("kind", "manual"))
        except ValueError:
            kind = QueueKind.MANUAL
        ctx = QueueContext(
            kind=kind,
            source_id=ctx_raw.get("source_id", ""),
            source_label=ctx_raw.get("source_label", ""),
            source_icon=ctx_raw.get("source_icon", ""),
        )
        items = data.get("original_items") or []
        play_order = data.get("play_order") or list(range(len(items)))
        current = data.get("current_index", -1)
        if current >= len(play_order):
            current = -1
        return cls(
            context=ctx,
            original_items=items,
            play_order=play_order,
            current_index=current,
            manual_overlay=data.get("manual_overlay") or [],
            is_modified=bool(data.get("is_modified", False)),
        )


@dataclass
class NowPlaying:
    item_id: str = ""
    # Stable identity of the cover artwork — for audio this is the
    # AlbumId (so every track on an album shares one cache slot in
    # the now-playing surfaces), otherwise the item id itself. Empty
    # = consumers should fall back to item_id.
    image_id: str = ""
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
    play_requested = Signal(object)        # NowPlaying
    pause_toggled = Signal()
    stop_requested = Signal()
    seek_requested = Signal(int)           # ms (absolute)
    seek_relative = Signal(int)            # ms (delta)
    volume_changed = Signal(int)           # 0–100
    mute_toggled = Signal()

    # ── Queue control ────────────────────────────────────────────────────────
    # Install a new queue. `context` is a `QueueContext` from
    # `modules.player_state`; emitters that don't care can pass
    # `QueueContext()` (defaults to MANUAL) but the now-playing page
    # relies on real values to pick the right pane content (album track
    # listing vs flat queue), so prefer being explicit.
    queue_play_now = Signal(list, int, object)  # items, start_index, context
    queue_add_next = Signal(list)               # items
    queue_add_end = Signal(list)                # items
    # Drag-reorder in the now-playing page's right pane. Both indices
    # are play-order indices (what the user sees in the rendered list);
    # QueueManager translates to original_items mutations.
    queue_move_item = Signal(int, int)          # src_play_idx, dest_play_idx
    # Right-click → "Remove from queue" on a track row in the
    # now-playing page. Index is play-order.
    queue_remove_at = Signal(int)               # play_idx
    queue_clear = Signal()
    next_track = Signal()
    prev_track = Signal()
    # `queue_changed` emits the play-ordered items + index into them, so
    # MPRIS / mini player / now-playing chrome don't have to know about
    # the underlying `Queue` model.
    queue_changed = Signal(list, int)           # full_queue (play order), index
    # Fires when the queue's source changes (album → playlist → shuffle …).
    # The queue page subscribes to this to repaint the right-pane heading
    # without re-rendering the whole track list on every jump.
    queue_context_changed = Signal(object)      # QueueContext
    track_jumped = Signal(int)                  # index in queue

    repeat_changed = Signal(str)
    shuffle_changed = Signal(bool)
    replaygain_changed = Signal(str)        # "no" | "track" | "album"
    lyrics_font_size_changed = Signal(str)  # "small" | "default" | "large" | "largest"
    # Hint to MpvController: the "next" track has changed. Carries either
    # a NowPlaying for the next item (so mpv can append it to its playlist
    # for gapless handoff) or None to signal "no next — drop any pending
    # prefetch." Emitted whenever the current track changes, when shuffle
    # toggles, and when the queue is mutated near the head.
    queue_prefetch_request = Signal(object)

    # ── State updates (backend → UI) ────────────────────────────────────────
    position_updated = Signal(int)         # ms
    duration_set = Signal(int)             # ms
    playback_started = Signal(object)      # NowPlaying
    playback_paused = Signal()
    playback_resumed = Signal()
    playback_stopped = Signal()
    playback_ended = Signal()
    # Fired once at app launch when a saved queue + saved position pair
    # restores: the UI shows the track + slider position as if paused,
    # but mpv hasn't loaded anything yet. The first play press reads
    # the carried NowPlaying.position and starts mpv at that offset.
    playback_restored = Signal(object)     # NowPlaying with .position set
    volume_state = Signal(int)
    mute_state = Signal(bool)

    # ── Cast ────────────────────────────────────────────────────────────────
    cast_started = Signal(str)
    cast_stopped = Signal()
    cast_devices_updated = Signal(list)

    # ── Favorite ────────────────────────────────────────────────────────────
    favorite_toggled = Signal(str, bool)   # item_id, is_favorite

    # ── UI ──────────────────────────────────────────────────────────────────
    open_main_window = Signal()
    show_mini_player = Signal()
    hide_mini_player = Signal()
    navigate_to_item = Signal(dict)        # item dict
    show_now_playing = Signal()
    notify_track = Signal(object)          # NowPlaying

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
