"""
Playback queue manager.

Owns a single `Queue` (jellytoast.player_state.Queue) and walks `play_order`
to drive playback. Shuffle is a permutation of `play_order`; the queue's
`original_items` is immutable for the queue's lifetime so the now-playing
page's right pane can render the source's natural order regardless of
shuffle state. Reference designs in `notes/queue-research.md` (Strawberry
+ Music Assistant lessons).
"""

import logging
from collections import deque
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, Slot

logger = logging.getLogger(__name__)
from jellytoast import async_io
from jellytoast.player_state import (
    NowPlaying,
    PlayerBus,
    Queue,
    QueueContext,
    QueueKind,
    RepeatMode,
    set_now_playing,
)
from jellytoast.providers import get_provider
from jellytoast.settings import get_settings
from jellytoast.smart_shuffle import artist_key as _artist_key
from jellytoast.smart_shuffle import smart_shuffle as _smart_shuffle

# How many recent (track, artist) pairs to keep around for smart_shuffle's
# recency penalty. Matches smart_shuffle._HISTORY_WINDOW * 4 so a few
# back-to-back shuffles still have meaningful seed data — the smart picker
# caps to its own window internally, this is just our retention.
_RECENT_HISTORY_SIZE = 32

# Seeded-radio feeder constants. See docs/research/radio_and_seeded_queues.md
# §5.2 for the design rationale.
_RADIO_REFILL_THRESHOLD = 5  # fire feeder within N of queue end
_RADIO_REFILL_BATCH = 25  # provider count= per refill
_RADIO_QUEUE_CAP = 200  # trim played items when above this
_RADIO_SKIP_WINDOW = 5  # transitions tracked for skip-heavy heuristic
_RADIO_SKIP_THRESHOLD = 3  # skips in window → advance offset on next refill


class QueueManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.api = get_provider()

        self._q = Queue()
        self._repeat: RepeatMode = RepeatMode(self.settings.repeat_mode)
        self._shuffle: bool = self.settings.shuffle
        # Rolling history of recently played artist ids, seeded into
        # smart_shuffle so the first picks of a fresh shuffle avoid
        # artists the user just heard. Session-only — a fresh launch
        # with a fresh deque is a feature, not a bug (per design doc).
        self._recent_artist_ids: deque = deque(maxlen=_RECENT_HISTORY_SIZE)
        # Seeded-radio feeder state. ``_refilling`` guards against
        # back-to-back ``_play_current`` events firing duplicate
        # provider calls while a refill is in flight; ``_skip_history``
        # is a rolling True/False window where True = user-initiated
        # skip and False = natural end-of-track. ≥3 skips in 5 →
        # next refill advances ``offset`` to break artist clusters
        # (§5.4 of the research doc).
        self._refilling: bool = False
        # Generation token bumped on every clear(). The radio refill
        # callback checks it before mutating ``self._q`` so a network
        # response that lands AFTER a user-initiated clear can't
        # resurrect the cleared queue with stale tracks.
        self._refill_gen: int = 0
        self._skip_history: deque = deque(maxlen=_RADIO_SKIP_WINDOW)

        # Debounced queue-persistence. Every mutation calls `_save()`,
        # which starts/restarts this timer; the actual write happens
        # 500 ms after the *last* mutation in a burst. Matters for
        # radio queues (200 tracks) where the refill feeder appends a
        # batch one append at a time — without the debounce each track
        # transition triggered a fresh full-payload write to disk.
        # `flush_pending_save()` synchronously runs any pending save;
        # the app's `aboutToQuit` cleanup calls it so the latest queue
        # state survives shutdown.
        self._save_debounce = QTimer(self)
        self._save_debounce.setSingleShot(True)
        self._save_debounce.setInterval(500)
        self._save_debounce.timeout.connect(self._actually_save)

        self._connect()

        # Restore previous queue. New format ships everything (context +
        # original items + play_order); old format (queue list + index)
        # comes back as a MANUAL context with sequential play_order.
        saved = self.settings.load_queue()
        if saved is not None:
            self._q = saved
            # Re-emit so chrome that subscribes at construction time
            # repopulates with the restored queue. Deferred via
            # QTimer.singleShot for the same reason ``playback_restored``
            # is below — the QueueManager is built BEFORE the bar / mini
            # player / NP page subscribe to the bus, so a synchronous
            # emit lands into the void. Without the defer, a restored
            # INTERNET_RADIO context never reaches the radio-cover
            # handlers and the resume-from-radio path shows no art.
            ctx_to_emit = self._q.context
            items_to_emit = self._q.play_ordered()
            index_to_emit = self._q.current_index
            QTimer.singleShot(
                0,
                lambda c=ctx_to_emit, items=items_to_emit, i=index_to_emit: (
                    self.bus.queue_context_changed.emit(c),
                    self.bus.queue_changed.emit(items, i),
                ),
            )
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
                if saved_id and saved_ms > 0 and current.get("Id") == saved_id:
                    np = self._build_now_playing(current)
                    np.position = saved_ms
                    np.is_paused = True
                    set_now_playing(np)
                    # Scheduled AFTER the queue_context_changed defer so
                    # subscribers process the radio context FIRST (their
                    # _is_radio flag is set, station logo loads) and then
                    # see playback_restored — preventing _on_started from
                    # trying to fetch a provider cover for the synthetic
                    # station id and clobbering the just-loaded logo.
                    QTimer.singleShot(0, lambda n=np: self.bus.playback_restored.emit(n))

    def _connect(self):
        self.bus.queue_play_now.connect(self.play_now)
        self.bus.queue_add_next.connect(self.add_next)
        self.bus.queue_add_end.connect(self.add_to_end)
        self.bus.queue_move_item.connect(self.move_item)
        self.bus.queue_remove_at.connect(self.remove_at)
        self.bus.queue_clear.connect(self.clear)
        # Bus-driven `next` is a user skip; `_on_playback_ended` (the
        # natural end of a track) calls `next` too but goes through a
        # different slot that flags the transition as natural. The
        # radio-feeder's skip-heavy heuristic reads these flags.
        self.bus.next_track.connect(self._on_user_next)
        self.bus.prev_track.connect(self.previous)
        self.bus.track_jumped.connect(self.jump_to)
        self.bus.repeat_changed.connect(self._on_repeat_changed)
        self.bus.shuffle_changed.connect(self._on_shuffle_changed)
        self.bus.playback_ended.connect(self._on_playback_ended)

    # ── Properties ──────────────────────────────────────────────────────────
    #
    # External readers (MPRIS, jellytoast/app.py click-suppression check, the
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
    def play_now(
        self, items: List[Dict], start_index: int = 0, context: Optional[QueueContext] = None
    ):
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
        cur_orig = (
            self._q.play_order[self._q.current_index]
            if 0 <= self._q.current_index < len(self._q.play_order)
            else len(self._q.original_items) - 1
        )
        insert_orig_at = cur_orig + 1
        insert_play_at = self._q.current_index + 1
        for i, item in enumerate(items):
            self._q.original_items.insert(insert_orig_at + i, item)
        # Shift any play_order indices that pointed past the insertion.
        shift = len(items)
        self._q.play_order = [(p + shift) if p >= insert_orig_at else p for p in self._q.play_order]
        for i in range(len(items)):
            self._q.play_order.insert(insert_play_at + i, insert_orig_at + i)
        self._q.is_modified = True
        self.bus.queue_context_changed.emit(self._q.context)
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
        self._q.is_modified = True
        self.bus.queue_context_changed.emit(self._q.context)
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        # Appending only changes the prefetch target when the current
        # track is the last entry in play_order; otherwise the existing
        # successor is unchanged. Re-emit so the prefetch picks up the
        # new tail entry.
        if base == self._q.current_index + 1:
            self._emit_prefetch()
        self._save()

    @Slot(int, int)
    def move_item(self, src_play_idx: int, dest_play_idx: int):
        """Reorder the queue: move the item at play-order index ``src``
        to play-order index ``dest``. Both indices are post-removal-aware
        (i.e. ``dest`` is the position the item should occupy in the
        final list, after the move). The currently-playing item stays
        the current item — its ``current_index`` is recomputed against
        the new play_order so playback doesn't skip.
        """
        n = self._q.length
        if not (0 <= src_play_idx < n) or not (0 <= dest_play_idx < n):
            return
        if src_play_idx == dest_play_idx:
            return
        cur_orig = (
            self._q.play_order[self._q.current_index] if 0 <= self._q.current_index < n else None
        )
        moved = self._q.play_order.pop(src_play_idx)
        self._q.play_order.insert(dest_play_idx, moved)
        if cur_orig is not None and cur_orig in self._q.play_order:
            self._q.current_index = self._q.play_order.index(cur_orig)
        self._q.is_modified = True
        self.bus.queue_context_changed.emit(self._q.context)
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        self._emit_prefetch()
        self._save()

    @Slot()
    def clear(self):
        self._q = Queue()
        # Invalidate any in-flight radio refill so its callback
        # doesn't re-append tracks to the now-empty queue.
        self._refill_gen += 1
        self._refilling = False
        # An empty queue means nothing is playing — stop mpv and clear the
        # global NowPlaying so a poller of get_now_playing() (e.g. the
        # sign-out path in session_controller) doesn't read the prior track.
        self.bus.stop_requested.emit()
        set_now_playing(NowPlaying())
        self.bus.queue_context_changed.emit(self._q.context)
        self.bus.queue_changed.emit([], -1)
        self._save()

    @Slot(int)
    def remove_at(self, play_index: int):
        """Remove the item at the given *play-order* index."""
        if not (0 <= play_index < self._q.length):
            return
        orig_idx = self._q.play_order.pop(play_index)
        # Rebuild original_items minus the removed entry, then reindex
        # play_order to account for the shift. Cheaper than tracking
        # tombstones for the small queues we deal with.
        del self._q.original_items[orig_idx]
        self._q.play_order = [(p - 1) if p > orig_idx else p for p in self._q.play_order]
        if play_index < self._q.current_index:
            self._q.current_index -= 1
        elif play_index == self._q.current_index:
            if self._q.current_index >= self._q.length:
                self._q.current_index = -1
                self.bus.stop_requested.emit()
                # Removing the playing tail track stops playback — clear the
                # global NowPlaying too, mirroring next()'s off-end path, so
                # get_now_playing() doesn't return the removed track.
                set_now_playing(NowPlaying())
            else:
                self._play_current()
        self._q.is_modified = True
        self.bus.queue_context_changed.emit(self._q.context)
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
            # End of queue with repeat off — stop mpv AND clear the
            # global NowPlaying state so any reader that polls
            # get_now_playing() (rather than subscribing to
            # playback_stopped) doesn't see a stale "last track".
            # current_index is deliberately *not* reset — leaving it
            # pointed at the last-played track means previous() can
            # walk back into the queue without the user having to
            # repopulate it (see test_off_last_emits_stop).
            self.bus.stop_requested.emit()
            from jellytoast.player_state import NowPlaying

            set_now_playing(NowPlaying())

    @Slot()
    def previous(self):
        if self._q.is_empty:
            return
        # If >3s into track, restart it; else go back.
        from jellytoast.player_state import get_now_playing

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

        Routes through ``smart_shuffle.smart_shuffle`` — a weighted
        picker that spreads artists out across the queue and weights
        against recent history. Preserves the anchored-head invariant.
        """
        if not self._q.original_items:
            return
        if keep_at_start:
            head = self._q.play_order[self._q.current_index]
            rest = [p for i, p in enumerate(self._q.play_order) if i != self._q.current_index]
            self._shuffle_rest(rest)
            self._q.play_order = [head] + rest
            self._q.current_index = 0
        else:
            if anchor_orig < 0 or anchor_orig >= len(self._q.original_items):
                rest = list(self._q.play_order)
                self._shuffle_rest(rest)
                self._q.play_order = rest
                self._q.current_index = 0
                return
            rest = [p for p in self._q.play_order if p != anchor_orig]
            self._shuffle_rest(rest)
            self._q.play_order = [anchor_orig] + rest
            self._q.current_index = 0

    def _shuffle_rest(self, rest: List[int]) -> None:
        """Permute ``rest`` (a list of original_items indices) in place
        via ``jellytoast.smart_shuffle`` — a weighted picker that spreads
        artists out and weights against tracks heard in the recent
        history window.

        Smart-shuffle works on items (it reads ArtistId), so we map the
        indices to items, shuffle, then map back. Identical items are
        disambiguated by their position in the original list — same
        item appearing twice in the queue keeps its distinct slot."""
        if len(rest) <= 1:
            return
        # Pair each original-items index with its item dict so
        # smart_shuffle can read ArtistId. The dict's id() is used as
        # the disambiguator on the way back so two items with the same
        # data don't collide.
        wrapped = [{**self._q.original_items[i], "_orig_index": i} for i in rest]
        ordered = _smart_shuffle(wrapped, list(self._recent_artist_ids))
        rest[:] = [w["_orig_index"] for w in ordered]

    # ── Internals ───────────────────────────────────────────────────────────

    @Slot()
    def _on_playback_ended(self):
        # Natural end of track — not a skip. Record before advancing
        # so the feeder's skip-heavy heuristic sees the transition
        # before the new `_play_current` runs.
        self._record_transition(was_skip=False)
        self.next()

    @Slot()
    def _on_user_next(self):
        """Bus-driven ``next_track`` — the user clicked skip. Records
        the transition as a skip before advancing so the radio feeder
        can react to skip-heavy patterns on the *next* refill."""
        self._record_transition(was_skip=True)
        self.next()

    def _record_transition(self, was_skip: bool) -> None:
        """Append one entry to the rolling skip-history window. Cheap
        and unconditional — the radio feeder only reads it when the
        current context is INSTANT_MIX, so manual / album queues pay
        nothing for it being tracked."""
        self._skip_history.append(bool(was_skip))

    def _play_current(self):
        item = self._q.current_item
        if not item:
            return
        # Feed the smart-shuffle history window with the SAME artist key
        # smart_shuffle uses for its spread penalty, so the recency
        # window and the spread penalty stay in lockstep. (Jellyfin song
        # items carry no scalar ArtistId — they key on AlbumArtist — so
        # the old ArtistId-only read never populated this on Jellyfin.)
        a_key = _artist_key(item)
        if a_key != "__unknown__":
            self._recent_artist_ids.append(a_key)
        # Seeded-radio bookkeeping: record the now-playing id in the
        # context's played set so the feeder dedupes refills against
        # everything heard this session. Must happen before the
        # within-5-of-end check below, otherwise the just-played track
        # could come back in the very next batch.
        item_id = str(item.get("Id") or "")
        if (
            item_id
            and self._q.context.kind == QueueKind.INSTANT_MIX
            and item_id not in self._q.context.radio_played_ids
        ):
            self._q.context.radio_played_ids.append(item_id)
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
        # Seeded-radio feeder runs *after* the now-playing pipeline so
        # the UI isn't blocked on a provider call. Cheap-noop for
        # non-radio queues — the kind check inside guards against
        # spurious provider calls.
        self._maybe_refill_radio()

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
        call — the slot no-ops if nothing actionable changed."""
        item = self._peek_next_item()
        np = self._build_now_playing(item) if item else None
        self.bus.queue_prefetch_request.emit(np)

    def _audio_stream_url(self, item_id: str) -> "tuple[str, bool]":
        """Resolve the URL to play for an audio item, returning
        ``(url, is_local)``. Prefers a downloaded local blob over the
        server stream when offline mode is on, the server is
        unreachable, or the user hasn't opted into "prefer server when
        online" — otherwise falls back to the server stream. Any
        failure (offline DB not ready, blob missing) degrades cleanly
        to the server stream."""
        try:
            from jellytoast import offline

            blob = offline.local_blob(item_id)
            if blob is not None and blob.exists():
                # Stream from the server only when the user opted in AND
                # the server is actually reachable AND we're not in
                # offline mode; in every other case the local copy wins.
                prefer_server = (
                    self.settings.prefer_server_when_online
                    and offline.is_server_reachable()
                    and not offline.is_offline_mode()
                )
                if not prefer_server:
                    return blob.as_uri(), True
        except Exception as e:
            logger.warning("local-blob check failed for %s: %s", item_id, e)
        return self.api.get_audio_stream_url(item_id), False

    def _build_now_playing(self, item: Dict) -> NowPlaying:
        item_id = item.get("Id", "")
        item_type = item.get("Type", "")

        # Internet-radio items carry the live HTTP/Icecast/HLS stream
        # URL inline (no provider round-trip needed) and skip the
        # downloaded-blob lookup entirely — there's no offline copy of
        # a live feed. The duration is 0; the bar replaces the seek bar
        # with elapsed + LIVE pip when the context kind reflects this.
        embedded_url = item.get("streamUrl") or ""
        is_local = False
        if embedded_url:
            stream_url = str(embedded_url)
        elif item_type == "Audio":
            stream_url, is_local = self._audio_stream_url(item_id)
        else:
            stream_url = self.api.get_video_stream_url(item_id)

        # Image: prefer album art for audio, primary for video. Radio
        # stations may carry a homePageUrl but no cover; thumb_url
        # falls through to an empty string when the cover-art lookup
        # has no id to bind to.
        if embedded_url:
            image_id = ""
            thumb_url = ""
        else:
            image_id = (
                item.get("AlbumId") if item_type == "Audio" and item.get("AlbumId") else item_id
            )
            thumb_url = self.api.get_image_url(image_id, "Primary", 600)
            image_id = image_id or ""

        if item_type == "Audio":
            artists = item.get("Artists", [])
            subtitle = ", ".join(artists) if artists else item.get("AlbumArtist", "")
        elif item_type == "Episode":
            subtitle = item.get("SeriesName", "")
        else:
            subtitle = str(item.get("ProductionYear") or "")

        return NowPlaying(
            item_id=item_id,
            image_id=image_id,
            title=item.get("Name", "Unknown"),
            subtitle=subtitle,
            album=item.get("Album", ""),
            year=str(item.get("ProductionYear") or ""),
            duration=(item.get("RunTimeTicks", 0) or 0) // 10_000,
            stream_url=stream_url,
            thumb_url=thumb_url,
            item_type=item_type,
            is_favorite=(item.get("UserData") or {}).get("IsFavorite", False),
            is_local=is_local,
            raw=item,
        )

    def _save(self):
        # Debounced — see `_save_debounce` setup in __init__. Mutations
        # that need their write durably on disk (i.e., right before a
        # shutdown path) should call `flush_pending_save()` afterward.
        self._save_debounce.start()

    def _actually_save(self):
        self.settings.save_queue(self._q)

    def flush_pending_save(self):
        """If a debounced save is pending, run it now. Safe to call
        when no save is queued — no-ops in that case."""
        if self._save_debounce.isActive():
            self._save_debounce.stop()
            self._actually_save()

    # ── Seeded-radio feeder ─────────────────────────────────────────────────
    #
    # docs/research/radio_and_seeded_queues.md §5.2 / §5.3 / §5.4. When the
    # active queue is an INSTANT_MIX (right-click "Start radio from…", "More
    # like this", artist-page "Radio") and the play head gets within 5 of
    # the tail, fetch a fresh batch from the seed, dedupe against
    # everything already in the queue + everything already played this
    # session, append, and trim the oldest played items if the total
    # exceeds 200. The whole thing runs off the GUI thread via
    # ``jellytoast.async_io.run_async`` so a slow provider call doesn't stall
    # track transitions.

    def _maybe_refill_radio(self) -> None:
        """Trigger a refill if the queue is a seeded radio and the play
        head is within ``_RADIO_REFILL_THRESHOLD`` of the end. No-ops on
        non-radio queues, while a refill is already in flight, or when
        there's no seed to refill from."""
        if self._q.context.kind != QueueKind.INSTANT_MIX:
            return
        if self._refilling:
            return
        # current_index is into play_order; refill when there are fewer
        # than threshold items ahead of (and including) the play head.
        remaining = self._q.length - 1 - self._q.current_index
        if remaining >= _RADIO_REFILL_THRESHOLD:
            return
        ctx = self._q.context
        if not ctx.source_id and ctx.seed_kind != "genre":
            return
        if ctx.seed_kind == "genre" and not ctx.source_label:
            return
        self._refilling = True
        # Snapshot what the worker needs — provider is resolved at
        # fetch time (not cached on self) per the singleton-refs rule
        # so a sign-out + relogin push of a fresh provider is picked
        # up without surgery here.
        seed_kind = ctx.seed_kind or "track"
        seed_id = ctx.source_id
        genre_name = ctx.source_label if seed_kind == "genre" else ""
        # Skip-heavy detection: if ≥3 of the last 5 transitions were
        # user skips, advance offset so the genre call (and the
        # similar / mix call's fallback page) returns a different
        # slice of the catalog. Subsonic getSimilarSongs2 doesn't
        # accept offset, so this only meaningfully helps genre radio
        # — but advancing it on the random-fallback path also
        # reduces the chance the fallback returns the same first
        # page on repeated empty refills.
        skip_count = sum(1 for s in self._skip_history if s)
        offset = (
            len(ctx.radio_played_ids)
            if skip_count >= _RADIO_SKIP_THRESHOLD
            else 0
        )
        played_snapshot = set(ctx.radio_played_ids)
        existing_ids = {
            str(it.get("Id") or "") for it in self._q.original_items if it.get("Id")
        }

        def _fetch():
            return self._radio_fetch_batch(seed_kind, seed_id, genre_name, offset)

        gen = self._refill_gen
        async_io.run_async(
            _fetch,
            on_result=lambda batch: self._on_refill_result(batch, played_snapshot, existing_ids, gen),
            on_error=lambda _exc: self._on_refill_error(),
        )

    def _radio_fetch_batch(
        self, seed_kind: str, seed_id: str, genre_name: str, offset: int
    ) -> List[Dict]:
        """Worker-thread provider call. Resolves the provider at call
        time (not at queue-install time) so a kind-switch / sign-out
        between queue install and refill picks up the new singleton.
        Returns ``[]`` on any miss / failure — the caller's empty path
        runs the random-library fallback."""
        api = get_provider()
        if seed_kind == "genre":
            return list(api.get_genre_radio(genre_name, count=_RADIO_REFILL_BATCH, offset=offset))
        # track / album / artist all resolve to instant_mix: Jellyfin
        # serves its curated /InstantMix sequence; Subsonic aliases
        # to getSimilarSongs2. Parity-clean: the queue manager never
        # branches on provider.kind().
        return list(api.get_instant_mix(seed_id, count=_RADIO_REFILL_BATCH))

    def _on_refill_result(
        self,
        batch: List[Dict],
        played_snapshot: set,
        existing_ids: set,
        gen: int = 0,
    ) -> None:
        """Provider call returned (on GUI thread). Dedupe, append, trim,
        emit. If the batch is empty, fall back to a random-library
        scoop scoped to the seed's library; if that's also empty, fire
        ``radio_exhausted`` and stop.

        ``gen`` is the refill-generation token captured at dispatch
        time. A clear() between dispatch and callback will have bumped
        ``self._refill_gen``; in that case the queue we'd be appending
        to is no longer the queue the user is looking at, so we drop
        the batch silently.
        """
        if gen != self._refill_gen:
            return
        if not batch:
            # Empty primary path — try the random-library fallback (§5.3).
            # Subsonic ignores parent_id when empty; Jellyfin scopes by
            # the seed's parent library if we had it cached, but we
            # don't track that here — empty parent works on both.
            try:
                fallback = list(get_provider().get_random_audio_items("", limit=_RADIO_REFILL_BATCH))
            except Exception:
                fallback = []
            if not fallback:
                self._refilling = False
                self.bus.radio_exhausted.emit()
                return
            batch = fallback
        # Dedupe against ids already played AND ids currently in the
        # queue. Order-preserving so the provider's intended sequence
        # survives. Empty ids (malformed items) get dropped.
        deduped: List[Dict] = []
        seen_in_batch: set = set()
        for item in batch:
            iid = str(item.get("Id") or "")
            if not iid or iid in played_snapshot or iid in existing_ids or iid in seen_in_batch:
                continue
            seen_in_batch.add(iid)
            deduped.append(item)
        if not deduped:
            self._refilling = False
            # The dedup may have wiped the whole batch — e.g. provider
            # returned the same 25 items already played. Don't fire
            # exhausted; the next track-change retries, possibly with
            # offset advanced by the skip-heavy heuristic.
            return
        # Append to original_items + play_order. Shuffle is preserved
        # by appending the new indices to the *end* of play_order —
        # the user's shuffled order keeps walking, then steps into
        # the fresh batch in arrival order. Re-shuffling the tail
        # would defeat the provider's intended sequence (Jellyfin
        # InstantMix returns a curated walk, not a random bag).
        base = len(self._q.original_items)
        self._q.original_items.extend(deduped)
        self._q.play_order.extend(range(base, base + len(deduped)))
        appended = len(deduped)
        self._trim_radio_overflow()
        self._refilling = False
        self.bus.queue_changed.emit(self._q.play_ordered(), self._q.current_index)
        self.bus.radio_extended.emit(appended)
        self._emit_prefetch()
        self._save()

    def _on_refill_error(self) -> None:
        """Provider call raised — clear the in-flight flag so the next
        track-change retries. Don't fire radio_exhausted here: a
        transient HTTP 500 shouldn't permanently kill the radio."""
        self._refilling = False

    def _trim_radio_overflow(self) -> None:
        """Cap total queue size at ``_RADIO_QUEUE_CAP`` by removing
        played items (position < current_index in play_order) from the
        head. Never trims the currently-playing or future tracks —
        always-played-only trim is the contract.
        """
        n = self._q.length
        if n <= _RADIO_QUEUE_CAP:
            return
        excess = n - _RADIO_QUEUE_CAP
        if excess <= 0:
            return
        # Walk play_order from index 0 up to current_index, collecting
        # trim candidates. play_order indices < current_index are
        # already played; cap candidates at `excess` so we trim only
        # what's needed.
        played_play_indices = list(range(min(self._q.current_index, n)))
        if not played_play_indices:
            return
        to_trim_play = played_play_indices[:excess]
        # Resolve play indices → original_items indices.
        to_trim_orig = sorted({self._q.play_order[p] for p in to_trim_play}, reverse=True)
        # Delete from original_items in reverse so earlier indices stay
        # valid. Drop matching entries from play_order, then reindex
        # remaining play_order entries to absorb the gap.
        removed_orig: set = set()
        for orig_idx in to_trim_orig:
            if 0 <= orig_idx < len(self._q.original_items):
                del self._q.original_items[orig_idx]
                removed_orig.add(orig_idx)
        # Rebuild play_order: drop entries pointing at removed indices,
        # and shift remaining indices down by however many removed
        # entries precede them.
        sorted_removed = sorted(removed_orig)

        def _shift(orig_i: int) -> int:
            shift = sum(1 for r in sorted_removed if r < orig_i)
            return orig_i - shift

        new_play_order: List[int] = []
        for p in self._q.play_order:
            if p in removed_orig:
                continue
            new_play_order.append(_shift(p))
        # current_index reflects how many play_order entries were
        # removed *before* it — since we only removed played entries,
        # the current track stays the current track, just at a
        # lower index.
        removed_before_current = sum(1 for p in to_trim_play if p < self._q.current_index)
        self._q.play_order = new_play_order
        self._q.current_index = max(0, self._q.current_index - removed_before_current)
