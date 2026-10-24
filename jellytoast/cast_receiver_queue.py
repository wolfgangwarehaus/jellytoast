"""Keep a Chromecast's own queue one track ahead of the app's.

Casting used to hand the receiver one track at a time and push the next
when it reported IDLE/FINISHED. Every track boundary then depended on our
connection being up at that moment, and a speaker group re-synced its
members on each new LOAD — a gap per track. Now, while a track plays, the
app's next track sits queued on the receiver (QUEUE_INSERT), the receiver
moves on by itself, and the app follows: when the receiver's media status
shows the queued item, the app advances its own queue as if the track had
ended (now-playing, play reports, scrobbles, queue position) without
pushing anything.

The app's queue stays the source of truth. Skip / previous / jump push
the track with a LOAD exactly as before (a LOAD replaces the receiver's
queue); reorder / shuffle / repeat / queue edits reach us as the
``queue_prefetch_request`` the mpv gapless path already uses, and we swap
the queued item (QUEUE_REMOVE + QUEUE_INSERT). Anything that can't be
kept in step falls back to the one-at-a-time push: internet radio (live,
never queued), a receiver that refuses the insert or the queued item, a
queued item we couldn't take back off (the app pushes its real next track
when the receiver reaches it), and a track end where the receiver never
moved on.

``CastReceiverQueue`` is a collaborator owned by ``MpvController``; it
reads the host's cast state and calls back into ``_on_ended`` /
``_cast_last_player_state``. The wire side is
``CastManager.chromecast_queue_next[_async]``.
"""

import logging
from typing import Optional

from PySide6.QtCore import QTimer

from jellytoast.cast_manager import CastType
from jellytoast.cast_manager._chromecast import CAST_TAG_KEY
from jellytoast.player_state import get_now_playing

logger = logging.getLogger(__name__)

_PLAYING = ("PLAYING", "BUFFERING", "PAUSED")


def _is_live(np) -> bool:
    return bool(np is not None and np.raw and np.raw.get("streamUrl"))


class CastReceiverQueue:
    # After the receiver says the playing track FINISHED, how long to wait
    # for it to report the queued one before pushing the next track
    # ourselves (it never took the queued item).
    ADVANCE_WAIT_S = 4.0
    # Retries for a request that reached a session a LOAD had just replaced.
    MAX_RETRIES = 3
    RETRY_MS = 1000

    def __init__(self, host):
        self._host = host
        # The app's next track (latest queue_prefetch_request), or None.
        self.wanted = None
        # What we have queued on the receiver: dict from
        # CastManager.chromecast_queue_next, or None.
        self.queued: Optional[dict] = None
        # Tags of items still on the receiver that the app no longer wants
        # next (we couldn't take them back off).
        self.stale_tags: set = set()
        # A LOAD (track push) is in flight — the receiver's queue is about
        # to be replaced, so don't touch it.
        self.loading = False
        # A queue request is in flight; one at a time.
        self.busy = False
        # Bumped whenever the receiver's queue is replaced or the session
        # ends; a reply from an older generation is dropped.
        self.gen = 0
        # Item the receiver just moved to; the next play() of it is a
        # handoff, not a push.
        self.followed_item_id = ""
        # Monotonic time the receiver said FINISHED with an item queued.
        self.end_wait_wall = 0.0
        # Item the receiver refused to queue; not retried until the
        # receiver's queue is replaced or the app's next track changes.
        self._refused_item_id = ""
        self._retries = 0

    # ── Lifecycle ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """The receiver's queue was replaced (a LOAD) or the session
        changed: forget everything queued."""
        self.gen += 1
        self.queued = None
        self.stale_tags.clear()
        self.busy = False
        self.loading = False
        self.followed_item_id = ""
        self.end_wait_wall = 0.0
        self._refused_item_id = ""
        self._retries = 0

    def begin_load(self) -> None:
        self.reset()
        self.loading = True

    def load_done(self, ok: bool) -> None:
        self.loading = False
        if ok:
            self.sync()

    def set_wanted(self, np) -> None:
        if np is None or self.wanted is None or np.item_id != self.wanted.item_id:
            self._refused_item_id = ""
        self.wanted = np
        self.sync()

    def take_followed(self, np) -> bool:
        """True (once) when ``np`` is the track the receiver already moved
        to by itself — play() then only updates the app."""
        if self.followed_item_id and np.item_id == self.followed_item_id:
            self.followed_item_id = ""
            return True
        return False

    @property
    def holding(self) -> bool:
        return self.end_wait_wall > 0

    # ── Keeping the receiver's queue in step ─────────────────────────────

    def _device(self):
        h = self._host
        cm = h._cast_manager
        if cm is None or cm.active_cast is None:
            return None
        if cm.active_cast.device_type != CastType.CHROMECAST:
            return None
        return cm.active_cast

    def _target(self):
        """The track that should be queued on the receiver, or None."""
        np = self.wanted
        if np is None or not np.stream_url or not np.is_audio or _is_live(np):
            return None
        current = get_now_playing()
        if not current.item_id or _is_live(current):
            return None
        # An end-of-track sleep timer stops after this track; the receiver
        # mustn't start the next one.
        if getattr(self._host, "_sleep_pending_eot", False):
            return None
        if np.item_id == self._refused_item_id:
            return None
        return np

    def sync(self) -> None:
        """Make the receiver's queued item match the app's next track."""
        if self.loading or self.busy:
            return
        dev = self._device()
        if dev is None:
            return
        want = self._target()
        q = self.queued
        if q is not None and want is not None and q["item_id"] == want.item_id:
            return
        if q is None and want is None:
            return
        remove_id = None
        if q is not None:
            if q.get("receiver_id") is None:
                # We can't name it to take it back off. It stays; when the
                # receiver gets to it, the app pushes its real next track.
                self.stale_tags.add(q["tag"])
                self.queued = None
                return
            remove_id = q["receiver_id"]
        elif self.stale_tags:
            # Anything queued now would play after the stale item.
            return
        self.busy = True
        gen = self.gen
        self._host._cast_manager.chromecast_queue_next_async(
            dev,
            want,
            provider=self._host.api,
            remove_item_id=remove_id,
            on_done=lambda result, _g=gen, _w=want: self._on_synced(_g, _w, result),
        )

    def _on_synced(self, gen: int, want, result: dict) -> None:
        if gen != self.gen:
            return
        self.busy = False
        if self.queued is not None:
            if result.get("removed"):
                self.queued = None
            else:
                self.stale_tags.add(self.queued["tag"])
                self.queued = None
                return
        queued = result.get("queued")
        if queued is not None:
            self.queued = queued
        elif result.get("retry") and self._retries < self.MAX_RETRIES:
            # Asked a session that had just been replaced; ask again.
            self._retries += 1
            QTimer.singleShot(self.RETRY_MS, self.sync)
            return
        elif want is not None and result.get("removed"):
            self._refused_item_id = want.item_id
        # The app's next track may have changed while we waited.
        self.sync()

    # ── Following the receiver ───────────────────────────────────────────

    def _on_queued_item(self, status, q) -> bool:
        tag = (getattr(status, "media_custom_data", None) or {}).get(CAST_TAG_KEY)
        if tag:
            return tag == q["tag"]
        # A receiver that drops customData: go by the URL, unless it's the
        # same URL as the track that was playing (a queued repeat).
        content_id = getattr(status, "content_id", None)
        return bool(content_id) and content_id == q["url"] and content_id != q.get(
            "playing_url"
        )

    def on_status(self, status) -> bool:
        """Look at a receiver status for a track change we didn't push.
        Returns True when the caller should stop processing this status."""
        q = self.queued
        ps = getattr(status, "player_state", None)
        if q is not None and self._on_queued_item(status, q):
            if ps == "IDLE" and (getattr(status, "idle_reason", "") or "") == "ERROR":
                logger.warning("cast: the receiver couldn't play the queued track")
                self._push_next_ourselves()
                return True
            # Moved on. The first status for the new item (seen on a Google
            # speaker group) is IDLE/FINISHED for the *old* item, carrying
            # the new one in extendedStatus (LOADING) — pychromecast takes
            # that as the media. Then LOADING, BUFFERING, PLAYING. Don't let
            # that IDLE reach the caller as a track end.
            self._follow(q)
            return ps not in _PLAYING
        tag = (getattr(status, "media_custom_data", None) or {}).get(CAST_TAG_KEY)
        if tag and tag in self.stale_tags and ps in _PLAYING and not self.loading:
            # The receiver moved on to a track the app no longer wants next.
            logger.info("cast: the receiver started a stale queued track; replacing it")
            self.stale_tags.clear()
            self._push_next_ourselves()
            return True
        if (
            q is not None
            and ps == "IDLE"
            and (getattr(status, "idle_reason", "") or "").upper() == "FINISHED"
            and self._host._cast_last_player_state in _PLAYING
        ):
            # The receiver may report the old track finished just before
            # it reports the queued one. Hold the advance; tick() decides.
            if not self.end_wait_wall:
                self.end_wait_wall = self._host._monotonic()
            return True
        return False

    def tick(self) -> None:
        """Poll-timer hook: give up on a receiver that finished a track
        but never moved on to the queued one."""
        if not self.end_wait_wall:
            return
        if self._host._monotonic() - self.end_wait_wall < self.ADVANCE_WAIT_S:
            return
        logger.info("cast: the receiver didn't move on to the queued track; pushing it")
        self._push_next_ourselves()

    def _follow(self, q) -> None:
        logger.debug("cast: the receiver moved on to %r", q["item_id"])
        self.queued = None
        self.end_wait_wall = 0.0
        self.followed_item_id = q["item_id"]
        # The track ended on the receiver: close it out and advance the
        # app's queue. play() sees followed_item_id and only updates the
        # app; the poll / next status anchors the new track's position.
        self._host._on_ended()
        self.followed_item_id = ""

    def _push_next_ourselves(self) -> None:
        """Fall back to the one-at-a-time path: end the track in the app,
        whose play() then LOADs the next one (replacing the receiver's
        queue)."""
        if self.queued is not None:
            self.stale_tags.add(self.queued["tag"])
        self.queued = None
        self.end_wait_wall = 0.0
        self._host._cast_last_player_state = "IDLE"
        self._host._on_ended()
