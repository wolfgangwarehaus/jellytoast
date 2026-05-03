"""Playback intent handler — the brain that turns a Jellyfin Web audio
request into a JellyToast queue install.

When a user clicks Play (or hits Shuffle, or auto-advances) inside the
embedded JF Web view, the WebEngine fires off a request to the audio
stream URL. ``_PlaybackInterceptor`` blocks that request and emits the
item id; this handler decides what to do with it:

1. **Cooldown gate** — JF Web's audio pipeline can fire 3-4 stale
   intents after we block the original request (REST + bitrate test +
   prefetch + retry, plus auto-advance through its own queue). For
   ``_QUEUE_COOLDOWN_S`` after a successful queue install, every
   intent is suppressed so the freshly-installed queue isn't
   clobbered by JF Web's pipeline trying to "complete" the blocked
   request.

2. **Queue introspection** — ask JF Web what its current playback
   queue looks like. If it returns a list with the intercepted item
   in it, install that queue (album, playlist, or whatever the user
   actually clicked). If it reports a freshly-clicked Shuffle button,
   call back into the host to do a true library-wide shuffle.

3. **Metadata fallback** — if JF Web's queue isn't reachable (e.g.
   we just silenced its <audio> element), fetch the item's metadata
   directly and expand it to its album, gated on a recent user click
   so auto-advance noise doesn't re-clobber an already-active queue.

Constants ``_QUEUE_COOLDOWN_S`` (1.5s — same lock as
``_METADATA_FALLBACK_SKIP_S``) and ``_USER_CLICK_FRESH_MS`` (1.2s)
are tuned so deliberate clicks pass and pipeline noise doesn't.

All collaborators (bus, queue manager, REST API, web page, web view)
are injected; the handler doesn't reach back into the host beyond a
single ``on_library_shuffle`` callback for "user clicked Shuffle".
"""

import json
import os
import re
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from modules.jellyfin_api import JellyfinAPI
from modules.player_state import PlayerBus, QueueContext, QueueKind


# Per-intent diagnostics. Mirrors the gate in `jellytoast.py` so toggling
# `JT_SHUFFLE_DEBUG=1` lights up both surfaces consistently.
_SHUFFLE_DEBUG = os.environ.get("JT_SHUFFLE_DEBUG") == "1"


class PlaybackIntentHandler(QObject):
    # Cross-thread silence trigger. `emit_queue` may be called from a
    # worker thread (library shuffle worker on the QThreadPool), but
    # `page.runJavaScript` only runs on the main thread. Emitting this
    # signal queues the call onto the GUI thread automatically.
    silence_jfweb_signal = Signal()

    # Matches a Jellyfin Web details-page id in the URL hash (e.g.
    # `#/details?id=<32hex>&context=playlists`). Used to recover the
    # surrounding context (playlist / album / artist) when the user
    # plays a track from a details page.
    _URL_CONTEXT_ID = re.compile(r"[?&]id=([a-f0-9]{32})", re.IGNORECASE)

    # Set to time.time() whenever we successfully install a queue. Used
    # to suppress JF Web's stale-request storm — when we block its
    # audio request, JF Web's pipeline (REST + bitrate test + prefetch
    # + audio.src load) can take 3-4 seconds before the in-flight load
    # finally errors and a fresh intent fires. Plus the player's
    # error-advance through *its* queue.
    _QUEUE_COOLDOWN_S = 1.5
    # Mirrors `_QUEUE_COOLDOWN_S` — covers the immediate burst of
    # same-item retries from the audio element after we block the
    # request. The longer-tail auto-advance through *different* items
    # is gated by `_USER_CLICK_FRESH_MS` instead.
    _METADATA_FALLBACK_SKIP_S = 1.5
    # Maximum age of the user's most recent JF Web click for an intent
    # to count as user-driven. Auto-advance fires from <audio> events
    # with no DOM click, so its click age grows monotonically — once it
    # exceeds this window the intent is treated as noise and refused.
    _USER_CLICK_FRESH_MS = 1200

    def __init__(self,
                 bus: PlayerBus,
                 queue_mgr,
                 api: JellyfinAPI,
                 page: QWebEnginePage,
                 view: QWebEngineView,
                 on_library_shuffle: Callable[[], None],
                 parent=None):
        super().__init__(parent)
        self.bus = bus
        self.queue_mgr = queue_mgr
        self.api = api
        self.page = page
        self.view = view
        self._on_library_shuffle = on_library_shuffle
        self._queue_set_at = 0.0
        self.silence_jfweb_signal.connect(self._do_silence_jfweb)

    # ── Public surface for host callers (e.g. library shuffle) ─────────────

    def is_in_cooldown(self) -> bool:
        """True while a fresh queue install should suppress new intents."""
        return (time.time() - self._queue_set_at) < self._QUEUE_COOLDOWN_S

    def stamp_cooldown(self):
        """Stamp the cooldown lock without installing a queue. Used by
        the library-shuffle entry point so any auto-advance intent
        firing during the shuffle fetch gets suppressed."""
        self._queue_set_at = time.time()

    def emit_queue(self, items: list, start: int, source: str,
                   context: "QueueContext | None" = None):
        """Centralized queue-set: stamps the cooldown timer, emits to
        the bus, then tells Jellyfin Web to halt its own playback so
        its auto-advance doesn't keep firing intents. `context` describes
        what kind of queue this is (album / playlist / shuffle / …) and
        drives the now-playing page right pane. Safe to call from worker
        threads — bus.queue_play_now and the silence signal both
        auto-queue across threads."""
        if not items:
            return
        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
        print(
            f"[JellyToast] queue set via {source}: {len(items)} items, "
            f"{len(unique_albums)} unique albums, start={start}",
            flush=True,
        )
        if context is None:
            context = QueueContext()
        self._queue_set_at = time.time()
        self.bus.queue_play_now.emit(items, start, context)
        self.silence_jfweb_signal.emit()

    # ── Intent flow ────────────────────────────────────────────────────────

    @Slot(str)
    def on_intent(self, item_id: str):
        # Suppress JF Web's auto-advance retries. After we block the
        # audio request for our intercepted track, JF Web's player
        # errors and advances through *its* queue, firing a fresh
        # intent for each retried track. Without this guard the last
        # one wins and our shuffle queue gets overwritten by the
        # original album.
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if _SHUFFLE_DEBUG:
            print(
                f"[JellyToast] _on_intent: item={item_id[:8]} "
                f"since_queue_set={since_set:.2f}s cooldown={self._QUEUE_COOLDOWN_S}s",
                flush=True,
            )
        if since_set < self._QUEUE_COOLDOWN_S:
            print(
                f"[JellyToast] suppressing intent {item_id[:8]} "
                f"(queue set {since_set:.2f}s ago)",
                flush=True,
            )
            return
        # Prefer Jellyfin Web's own playback queue — it reflects whatever
        # the user actually triggered (shuffle library / album / playlist /
        # search result / single track). Falls back to manual context
        # expansion if the queue isn't reachable.
        self.page.runJavaScript(
            "window.__jellytoast_queue_state ? window.__jellytoast_queue_state() : null;",
            lambda result: self._on_queue_state(item_id, result),
        )
    def _do_silence_jfweb(self):
        # Run the silence script repeatedly: the <audio> element JF Web
        # uses might not exist at the instant we install our queue
        # (the htmlAudioPlayer plugin lazy-creates it on first play),
        # so a single pass can miss it and let auto-advance leak. The
        # 0/200/600/1200ms ladder catches whatever creation timing JF
        # Web uses without flooding the page with runJavaScript calls.
        for delay in (0, 200, 600, 1200, 2400):
            QTimer.singleShot(
                delay,
                lambda: self.page.runJavaScript(
                    "window.__jellytoast_silence_jfweb && "
                    "window.__jellytoast_silence_jfweb();"
                ),
            )

    def _on_queue_state(self, item_id: str, payload):
        # _on_intent's cooldown gate runs before runJavaScript is
        # dispatched, but the runJavaScript itself is async — by the
        # time this callback fires, a queue may have been installed
        # by the bridge fast-path. Re-check cooldown here so we don't
        # overwrite a fresh queue with stale state. Also avoids the
        # metadata-fallback path: when we silenced JF Web, its
        # pm.playlist() returns null and we'd otherwise fetch the
        # intercepted track and expand it to its album.
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if since_set < self._QUEUE_COOLDOWN_S:
            print(
                f"[JellyToast] queue_state callback within cooldown "
                f"({since_set:.2f}s) — discarding",
                flush=True,
            )
            return
        url = self.view.url().toString()
        if _SHUFFLE_DEBUG:
            print(f"[JellyToast] intent on URL: {url}", flush=True)
        click_age_ms = 999999
        if payload:
            try:
                data = json.loads(payload)
                shuffle_intent = bool(data.get("shuffle"))
                items = data.get("items") or []
                idx = int(data.get("index") or 0)
                click_age_ms = int(data.get("click_age_ms") or 999999)
                src_id = (data.get("source_id") or "").strip()
                src_type = (data.get("source_type") or "").strip()
                src_name = (data.get("source_name") or "").strip()

                # Primary signal: user just clicked a Shuffle button.
                # Forced library-wide shuffle. Stamp is consumed JS-side
                # so JF Web's auto-advance retries don't re-trigger.
                if shuffle_intent:
                    print("[JellyToast] shuffle click detected", flush=True)
                    self._on_library_shuffle()
                    return

                # Tile-attribution override: if the user clicked the
                # center play overlay on a Playlist tile, JF Web may
                # queue the parent album's tracks rather than the
                # playlist's actual tracks (observed: a 3-track
                # playlist whose first track is from a 13-track album
                # gets the full album queued). Fetch the authoritative
                # items from the API and override.
                #
                # Type detection: JS sends src_type when the tile DOM
                # carries data-type, but several JF Web card variants
                # strip it. When missing, fall back to api.get_item —
                # the result is cached so the lookup is essentially
                # free after the first hit. Skip the lookup when the
                # src_id IS the played item (clicking a track tile
                # directly — the source is the track itself, not a
                # parent collection).
                if src_id and src_id.lower() != item_id.lower():
                    if not src_type:
                        try:
                            src_meta = self.api.get_item(src_id) or {}
                            src_type = src_meta.get("Type", "")
                            if not src_name:
                                src_name = src_meta.get("Name", "")
                        except Exception as e:
                            print(f"[JellyToast] src item lookup failed: {e}", flush=True)
                    if src_type == "Playlist":
                        try:
                            pl_tracks = self.api.get_playlist_items(src_id)
                        except Exception as e:
                            print(f"[JellyToast] playlist fetch failed: {e}", flush=True)
                            pl_tracks = []
                        if pl_tracks:
                            target = item_id.lower()
                            start = next(
                                (i for i, t in enumerate(pl_tracks)
                                 if (t.get("Id") or "").lower() == target),
                                0,
                            )
                            label = src_name
                            if not label:
                                try:
                                    label = (self.api.get_item(src_id) or {}).get("Name", "")
                                except Exception:
                                    label = ""
                            ctx = QueueContext(
                                kind=QueueKind.PLAYLIST,
                                source_id=src_id,
                                source_label=label,
                            )
                            self.emit_queue(pl_tracks, start,
                                            "playlist tile click", context=ctx)
                            return

                if items:
                    if _SHUFFLE_DEBUG:
                        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
                        print(
                            f"[JellyToast] JF Web queue: {len(items)} tracks, "
                            f"{len(unique_albums)} unique album(s), "
                            f"library_view={self._is_library_view()}",
                            flush=True,
                        )
                    target = item_id.lower()
                    if 0 <= idx < len(items) and (items[idx].get("Id") or "").lower() == target:
                        start = idx
                    else:
                        start = next(
                            (i for i, it in enumerate(items)
                             if (it.get("Id") or "").lower() == target),
                            -1,
                        )
                    if start >= 0:
                        # Prefer the URL context — if the user is on a
                        # Playlist or MusicAlbum detail page, that's the
                        # truth, regardless of how the items happen to
                        # be arranged. Falsy AlbumId-uniformity inference
                        # was misclassifying single-artist playlists as
                        # albums (every track shared the same AlbumId
                        # because the playlist happened to draw from one
                        # album), which routed them through the album-
                        # shaped track list (single-artist rows + disc
                        # dividers) instead of the playlist-shaped one.
                        url_ctx = self._fetch_url_context(exclude_id=item_id)
                        url_type = (url_ctx or {}).get("Type", "")
                        if url_type == "Playlist":
                            ctx = QueueContext(
                                kind=QueueKind.PLAYLIST,
                                source_id=url_ctx.get("Id", ""),
                                source_label=url_ctx.get("Name", ""),
                            )
                        elif url_type == "MusicAlbum":
                            ctx = QueueContext(
                                kind=QueueKind.ALBUM,
                                source_id=url_ctx.get("Id", ""),
                                source_label=url_ctx.get("Name", ""),
                            )
                        else:
                            # No useful URL context — fall back to
                            # heuristics. Playlists never visit a
                            # details page when the user clicks the
                            # tile's center play button (the URL stays
                            # at /playlists.html), so we have to infer.
                            #
                            # Duplicate-track guard: a real album never
                            # repeats a track, so any duplicates in the
                            # items list mean this can't be an album,
                            # regardless of AlbumId uniformity.
                            ids = [it.get("Id") for it in items if it.get("Id")]
                            has_duplicates = len(ids) != len(set(ids))
                            album_ids = {
                                it.get("AlbumId") for it in items
                                if it.get("AlbumId")
                            }
                            if not has_duplicates and len(album_ids) == 1:
                                (only_album,) = album_ids
                                ctx = QueueContext(
                                    kind=QueueKind.ALBUM,
                                    source_id=only_album or "",
                                    source_label=items[0].get("Album", ""),
                                )
                            else:
                                # Multi-album OR has duplicates — treat
                                # as a free-form queue. The now-playing
                                # page will render rows with per-track
                                # artist sub-lines (cross-artist queues
                                # already get this treatment) and skip
                                # the disc dividers.
                                ctx = QueueContext(kind=QueueKind.MANUAL)
                        self.emit_queue(items, start, "JF Web queue",
                                          context=ctx)
                        return
            except Exception as e:
                print(f"[JellyToast] queue_state parse failed: {e}", flush=True)

        # The destructive metadata fallback is the only thing JF Web's
        # silent auto-advance can ride past our queue cooldown. Gate it
        # on a fresh user-initiated click — auto-advance is driven by
        # <audio> events with zero DOM input, so its click_age stays
        # large. Without a recent click we treat the intent as noise
        # and refuse to clobber the active queue.
        if self.queue_mgr.queue and click_age_ms > self._USER_CLICK_FRESH_MS:
            if _SHUFFLE_DEBUG:
                print(
                    f"[JellyToast] suppressing intent {item_id[:8]} — "
                    f"no fresh click (age={click_age_ms}ms) and queue is owned",
                    flush=True,
                )
            return
        self._intent_via_metadata(item_id)

    def _is_library_view(self) -> bool:
        url = self.view.url().toString()
        if "#" not in url:
            return False
        hash_part = url.split("#", 1)[1].lower()
        # A details page is never a library view, even if a topParentId
        # query param tags along for breadcrumbs.
        if "/details" in hash_part:
            return False
        # Library list pages across Jellyfin Web versions use one of:
        #   #/music.html?topParentId=<id>   (10.10 and earlier)
        #   #/list.html?type=MusicAlbum&parentId=<id>
        #   #/music?topParentId=<id>        (newer routing)
        # Any of these markers identifies a list page.
        markers = (
            "music.html", "movies.html", "tv.html", "tvshows.html",
            "list.html",
            "topparentid=", "parentid=",
        )
        return any(m in hash_part for m in markers)
    def _intent_via_metadata(self, item_id: str):
        # Metadata fallback expands the intercepted track to its album.
        # That's the right move when we have no queue yet (first launch,
        # post-stop) AND when the user clicks a different album after a
        # shuffle (the click's intent fires past the 5s queue cooldown,
        # so we honor it as a deliberate switch).
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if 0 < since_set < self._METADATA_FALLBACK_SKIP_S:
            print(
                f"[JellyToast] metadata fallback skipped — queue installed "
                f"{since_set:.1f}s ago",
                flush=True,
            )
            return
        try:
            item = self.api.get_item(item_id)
        except Exception as e:
            print(f"[JellyToast] metadata fetch failed for {item_id}: {e}", flush=True)
            return
        if not item:
            return
        context_item = self._fetch_url_context(exclude_id=item_id)
        items, start_idx, q_context = self._expand_context(item, context_item)
        self.emit_queue(items, start_idx, "metadata fallback", context=q_context)

    def _fetch_url_context(self, exclude_id: str = "") -> dict | None:
        url = self.view.url().toString()
        if "#" not in url:
            return None
        m = self._URL_CONTEXT_ID.search(url.split("#", 1)[1])
        if not m:
            return None
        ctx_id = m.group(1).lower()
        if exclude_id and ctx_id == exclude_id.lower():
            # The played item *is* the context (e.g. user clicked Play on
            # a single track's own details page). Nothing extra to fetch.
            return None
        try:
            return self.api.get_item(ctx_id)
        except Exception as e:
            print(f"[JellyToast] context fetch failed for {ctx_id}: {e}", flush=True)
            return None

    def _expand_context(self, item: dict, context_item: dict | None = None):
        """For an audio track, queue the surrounding playlist or album so
        Next/Prev walk the right context. Returns (items, start_index,
        QueueContext) — the context tags the queue source so the now-
        playing page knows whether to render an album track listing or
        a playlist, etc."""
        if item.get("Type") != "Audio":
            return [item], 0, QueueContext(
                kind=QueueKind.MANUAL, source_label=item.get("Name", ""),
            )

        # Playlist context — user clicked a track from a playlist's
        # details page. Queue the whole playlist starting at this track.
        if context_item and context_item.get("Type") == "Playlist":
            try:
                tracks = self.api.get_playlist_items(context_item["Id"])
                if tracks:
                    items, start_idx = self._index_starting_at(tracks, item.get("Id"))
                    return items, start_idx, QueueContext(
                        kind=QueueKind.PLAYLIST,
                        source_id=context_item.get("Id", ""),
                        source_label=context_item.get("Name", ""),
                    )
            except Exception as e:
                print(f"[JellyToast] playlist expand failed: {e}", flush=True)

        # Album context (default) — queue the track's own album.
        if item.get("AlbumId"):
            try:
                tracks = self.api.get_album_tracks(item["AlbumId"])
                if tracks:
                    # get_album_tracks doesn't return AlbumId by default —
                    # propagate it from the original item so every track's
                    # _build_now_playing can resolve the album art
                    # reliably (the track's own /Items/{id}/Images/Primary
                    # is inconsistent across Jellyfin versions).
                    album_id = item["AlbumId"]
                    for t in tracks:
                        t.setdefault("AlbumId", album_id)
                    items, start_idx = self._index_starting_at(tracks, item.get("Id"))
                    return items, start_idx, QueueContext(
                        kind=QueueKind.ALBUM,
                        source_id=album_id,
                        source_label=item.get("Album", ""),
                    )
            except Exception as e:
                print(f"[JellyToast] album expand failed: {e}", flush=True)

        return [item], 0, QueueContext(
            kind=QueueKind.MANUAL, source_label=item.get("Name", ""),
        )

    @staticmethod
    def _index_starting_at(tracks: list[dict], item_id: str) -> tuple[list[dict], int]:
        for i, t in enumerate(tracks):
            if t.get("Id") == item_id:
                return tracks, i
        return tracks, 0
