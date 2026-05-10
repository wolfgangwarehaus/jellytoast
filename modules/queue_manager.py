"""
Playback queue manager.

Owns a single `Queue` (modules.player_state.Queue) and walks `play_order`
to drive playback. Shuffle is a permutation of `play_order`; the queue's
`original_items` is immutable for the queue's lifetime so the now-playing
page's right pane can render the source's natural order regardless of
shuffle state. Reference designs in `notes/queue-research.md` (Strawberry
+ Music Assistant lessons).
"""

import random
from typing import List, Dict, Optional
from PySide6.QtCore import QObject, Slot
from modules.player_state import (
    PlayerBus, RepeatMode, NowPlaying, Queue, QueueContext, QueueKind,
    set_now_playing,
)
from modules.settings import get_settings
from modules.providers import get_provider


class QueueManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_provider()

        self._q = Queue()
        self._repeat: RepeatMode = RepeatMode(self.settings.repeat_mode)
        self._shuffle: bool = self.settings.shuffle

        self._connect()

        # Restore previous queue. New format ships everything (context +
        # original items + play_order); old format (queue list + index)
        # comes back as a MANUAL context with sequential play_order.
        saved = self.settings.load_queue()
        if saved is not None:
            self._q = saved
            # Re-emit so chrome that subscribes at construction time
            # repopulates with the restored queue.
            self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
            self.bus.queue_context_changed.emit(self._q.context)
            # Resume position: if the last persisted position belongs to
            # the queue's current item, surface it so the bar shows
            # "paused at 1:30" instead of empty. Tying the position to
            # an item_id guards against stale positions when the queue
            # advanced without a clean shutdown — id mismatch means we
            # ignore the position rather than start a different track
            # mid-way through.
            current = self._q.current_item
            if current is not None:
                saved_id = self.settings.saved_position_item_id
                saved_ms = self.settings.saved_position_ms
                if (saved_id and saved_ms > 0
                        and current.get("Id") == saved_id):
                    np = self._build_now_playing(current)
                    np.position = saved_ms
                    np.is_paused = True
                    set_now_playing(np)
                    # Defer the emit until after the rest of app
                    # construction completes — chrome that subscribes
                    # at widget-construction time (now_playing_bar,
                    # mini_player, MPRIS) needs to be wired before the
                    # signal fires, otherwise the restore lands into
                    # the void and the bar shows "Nothing playing"
                    # despite the live state being set.
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(
                        0, lambda n=np: self.bus.playback_restored.emit(n)
                    )

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
    #
    # External readers (MPRIS, jellytoast.py click-suppression check, the
    # mini player) only need the play-ordered view. The `Queue` model lives
    # internally; we expose flat lists here so consumers don't have to learn
    # the play_order indirection.

    @property
    def queue(self) -> List[Dict]:
        """Items in play order — what the mini player and MPRIS render."""
        return self._q.play_ordered()

    @property
    def current_index(self) -> int:
        return self._q.current_index

    @property
    def current_item(self) -> Optional[Dict]:
        return self._q.current_item

    @property
    def context(self) -> QueueContext:
        return self._q.context

    @property
    def original_items(self) -> List[Dict]:
        """The queue's source-order items — what the album right pane wants
        to render. For a manual / shuffle queue this is the same as
        `queue` but in insertion order."""
        return list(self._q.original_items)

    @property
    def has_next(self) -> bool:
        if self._repeat == RepeatMode.ALL or self._repeat == RepeatMode.ONE:
            return self._q.length > 0
        return self._q.current_index < self._q.length - 1

    @property
    def has_previous(self) -> bool:
        if self._repeat == RepeatMode.ALL:
            return self._q.length > 0
        return self._q.current_index > 0

    @property
    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    @property
    def shuffle_enabled(self) -> bool:
        return self._shuffle

    # ── Mutations ───────────────────────────────────────────────────────────

    @Slot(list, int, object)
    def play_now(self, items: List[Dict], start_index: int = 0,
                 context: Optional[QueueContext] = None):
        """Replace the queue with `items`, starting from `start_index`
        (in source order). `context` describes the source (album / playlist
        / shuffle / …) and drives the now-playing page's pane selection."""
        if not items:
            return
        if context is None:
            context = QueueContext()  # MANUAL default
        self._q = Queue(
            context=context,
            original_items=list(items),
            play_order=list(range(len(items))),
            current_index=max(0, min(start_index, len(items) - 1)),
        )
        if self._shuffle:
            self._apply_shuffle(keep_at_start=True)
        self.bus.queue_context_changed.emit(self._q.context)
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        self._play_current()

    @Slot(list)
    def add_next(self, items: List[Dict]):
        if not items:
            return
        if self._q.is_empty:
            self.play_now(items, 0, QueueContext(kind=QueueKind.MANUAL))
            return
        # Insert into both original_items (right after the currently-playing
        # source-order index) and play_order (right after current_index).
        # See notes/queue-research.md — Strawberry's queue overlay is the
        # cleaner long-term answer; for now we mutate the queue in place
        # and accept that "add next" promotes the context's pristineness.
        cur_orig = self._q.play_order[self._q.current_index] if 0 <= self._q.current_index < len(self._q.play_order) else len(self._q.original_items) - 1
        insert_orig_at = cur_orig + 1
        insert_play_at = self._q.current_index + 1
        for i, item in enumerate(items):
            self._q.original_items.insert(insert_orig_at + i, item)
        # Shift any play_order indices that pointed past the insertion.
        shift = len(items)
        self._q.play_order = [
            (p + shift) if p >= insert_orig_at else p
            for p in self._q.play_order
        ]
        for i in range(len(items)):
            self._q.play_order.insert(insert_play_at + i, insert_orig_at + i)
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        self._emit_prefetch()
        self._save()

    @Slot(list)
    def add_to_end(self, items: List[Dict]):
        if not items:
            return
        was_empty = self._q.is_empty
        if was_empty:
            self.play_now(items, 0, QueueContext(kind=QueueKind.MANUAL))
            return
        base = len(self._q.original_items)
        self._q.original_items.extend(items)
        self._q.play_order.extend(range(base, base + len(items)))
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        # Adding at the end only changes "next" when the queue had a
        # single item before — re-emit so the prefetch picks up the new
        # tail entry.
        if base == self._q.current_index + 1:
            self._emit_prefetch()
        self._save()

    @Slot()
    def clear(self):
        self._q = Queue()
        self.bus.queue_context_changed.emit(self._q.context)
        self.bus.queue_changed.emit([], -1)
        self._save()

    def remove_at(self, play_index: int):
        """Remove the item at the given *play-order* index."""
        if not (0 <= play_index < self._q.length):
            return
        orig_idx = self._q.play_order.pop(play_index)
        # Rebuild original_items minus the removed entry, then reindex
        # play_order to account for the shift. Cheaper than tracking
        # tombstones for the small queues we deal with.
        del self._q.original_items[orig_idx]
        self._q.play_order = [
            (p - 1) if p > orig_idx else p
            for p in self._q.play_order
        ]
        if play_index < self._q.current_index:
            self._q.current_index -= 1
        elif play_index == self._q.current_index:
            if self._q.current_index >= self._q.length:
                self._q.current_index = -1
                self.bus.stop_requested.emit()
            else:
                self._play_current()
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        self._save()

    # ── Navigation ──────────────────────────────────────────────────────────

    @Slot()
    def next(self):
        if self._q.is_empty:
            return
        if self._repeat == RepeatMode.ONE:
            self._play_current()
            return
        if self._q.current_index < self._q.length - 1:
            self._q.current_index += 1
            self._play_current()
        elif self._repeat == RepeatMode.ALL:
            self._q.current_index = 0
            self._play_current()
        else:
            self.bus.stop_requested.emit()

    @Slot()
    def previous(self):
        if self._q.is_empty:
            return
        # If >3s into track, restart it; else go back.
        from modules.player_state import get_now_playing
        np = get_now_playing()
        if np.position > 3000:
            self.bus.seek_requested.emit(0)
            return
        if self._q.current_index > 0:
            self._q.current_index -= 1
            self._play_current()
        elif self._repeat == RepeatMode.ALL:
            self._q.current_index = self._q.length - 1
            self._play_current()
        else:
            self.bus.seek_requested.emit(0)

    @Slot(int)
    def jump_to(self, play_index: int):
        if 0 <= play_index < self._q.length:
            self._q.current_index = play_index
            self._play_current()

    # ── Repeat / Shuffle ────────────────────────────────────────────────────

    @Slot(str)
    def _on_repeat_changed(self, mode: str):
        self._repeat = RepeatMode(mode)
        self.settings.repeat_mode = mode

    @Slot(bool)
    def _on_shuffle_changed(self, enabled: bool):
        self._shuffle = enabled
        self.settings.shuffle = enabled
        if self._q.is_empty:
            return
        # Preserve the currently-playing item's identity across the
        # play_order rewrite — the user expects the song they're listening
        # to to keep playing through the toggle.
        cur_orig_idx = (
            self._q.play_order[self._q.current_index]
            if 0 <= self._q.current_index < len(self._q.play_order)
            else 0
        )
        if enabled:
            self._apply_shuffle(keep_at_start=False, anchor_orig=cur_orig_idx)
        else:
            self._q.play_order = list(range(len(self._q.original_items)))
            try:
                self._q.current_index = self._q.play_order.index(cur_orig_idx)
            except ValueError:
                self._q.current_index = 0
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        # Toggling shuffle changes which track is "next" without changing
        # what's playing — refresh mpv's prefetch slot so gapless points
        # at the right successor.
        self._emit_prefetch()
        self._save()

    def _apply_shuffle(self, keep_at_start: bool, anchor_orig: int = -1):
        """Permute `play_order` in place. With `keep_at_start=True`, the
        item at `current_index` becomes index 0 of the shuffled order
        (used when installing a fresh shuffle queue so playback starts on
        the requested track). Otherwise `anchor_orig` (an original_items
        index) is preserved at the head.
        """
        if not self._q.original_items:
            return
        if keep_at_start:
            head = self._q.play_order[self._q.current_index]
            rest = [p for i, p in enumerate(self._q.play_order)
                    if i != self._q.current_index]
            random.shuffle(rest)
            self._q.play_order = [head] + rest
            self._q.current_index = 0
        else:
            if anchor_orig < 0 or anchor_orig >= len(self._q.original_items):
                random.shuffle(self._q.play_order)
                self._q.current_index = 0
                return
            rest = [p for p in self._q.play_order if p != anchor_orig]
            random.shuffle(rest)
            self._q.play_order = [anchor_orig] + rest
            self._q.current_index = 0

    # ── Internals ───────────────────────────────────────────────────────────

    @Slot()
    def _on_playback_ended(self):
        self.next()

    def _play_current(self):
        item = self._q.current_item
        if not item:
            return
        np = self._build_now_playing(item)
        set_now_playing(np)
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        # Fire playback_started BEFORE play_requested so the bar / mini /
        # np-page kick off cover loads and metadata updates immediately,
        # rather than waiting the ~50-200ms mpv.play takes to return and
        # emit its own playback_started. player_backend still emits the
        # signal again after mpv.play succeeds — the duplicate is
        # absorbed (cover load_image_async dedups on cache_key, label
        # text re-sets are idempotent). Net effect: the bottom-bar
        # cover starts fetching as early as possible in the chain.
        self.bus.playback_started.emit(np)
        self.bus.play_requested.emit(np)
        # Tell mpv what's next so libmpv can prefetch it for gapless
        # handoff. Order matters: play_requested first (mpv loads the
        # current track), then prefetch_request (mpv appends next to
        # its playlist) — appending before mpv has anything playing
        # would just queue two cold starts back-to-back.
        self._emit_prefetch()
        self._save()

    def _peek_next_item(self) -> Optional[Dict]:
        """Return the item that would play after `next()` is called, or
        None if there isn't one. Honors RepeatMode.ONE (replay current)
        and RepeatMode.ALL (wrap to start)."""
        if self._q.is_empty or self._q.current_index < 0:
            return None
        if self._repeat == RepeatMode.ONE:
            return self._q.current_item
        next_idx = self._q.current_index + 1
        if next_idx < self._q.length:
            return self._q.original_items[self._q.play_order[next_idx]]
        if self._repeat == RepeatMode.ALL and self._q.length > 0:
            return self._q.original_items[self._q.play_order[0]]
        return None

    def _emit_prefetch(self):
        """Emit the next-track NowPlaying (or None) so MpvController can
        keep mpv's playlist primed for gapless transitions. Cheap to
        call — the slot no-ops if nothing actionable changed.
        Suppressed when the user disables gapless playback so mpv loads
        each track fresh on advance instead of pre-buffering the next."""
        if not self.settings.gapless:
            self.bus.queue_prefetch_request.emit(None)
            return
        item = self._peek_next_item()
        np = self._build_now_playing(item) if item else None
        self.bus.queue_prefetch_request.emit(np)

    def _build_now_playing(self, item: Dict) -> NowPlaying:
        item_id = item.get("Id", "")
        item_type = item.get("Type", "")

        if item_type == "Audio":
            stream_url = self.api.get_audio_stream_url(item_id)
        else:
            stream_url = self.api.get_video_stream_url(item_id)

        # Image: prefer album art for audio, primary for video.
        image_id = item.get("AlbumId") if item_type == "Audio" and item.get("AlbumId") else item_id
        thumb_url = self.api.get_image_url(image_id, "Primary", 600)
        image_id = image_id or ""

        if item_type == "Audio":
            artists = item.get("Artists", [])
            subtitle = ", ".join(artists) if artists else item.get("AlbumArtist", "")
        elif item_type == "Episode":
            subtitle = item.get("SeriesName", "")
        else:
            subtitle = str(item.get("ProductionYear", ""))

        return NowPlaying(
            item_id=item_id,
            image_id=image_id,
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
        self.settings.save_queue(self._q)
