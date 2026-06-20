"""Fetch / paging state machine for the library grid.

The pagination + disk-cache + silent-refresh + tail-growth-probe machinery,
extracted from ``library_grid.py``. This is the code that carried the
album-doubling / truncation races, so it is guarded by a generation token
(``self._load_gen``) and a re-entrancy latch (``self._loading_more``); those
are preserved exactly.

``_PaginatorMixin`` is mixed into ``LibraryGrid`` — it is *not* standalone.
Its methods reference page-owned state/widgets (``self._model``, ``self._view``,
``self._load_gen``, ``self._loaded_count``, ``self._refresh_scope`` …) set up by
``LibraryGrid.__init__``, and call back into cover/alphabet/empty-state methods
that stay on the grid (``self._load_visible_covers``, ``self._fire_cover_load``,
``self._show_empty_state``, ``self._index_letter_for`` …). Both directions
resolve on the combined instance. The ``_items_loaded`` / ``_refresh_loaded``
Signals stay declared on ``LibraryGrid`` (Signals need QObject ancestry); these
methods emit them via ``self``.

NB ``run_async`` is imported here, so tests that stub the fetch path must patch
``library_paginator.run_async`` (not ``library_grid.run_async``). The
``QTimer.singleShot`` cascades mutate the shared QTimer class, so those patches
work through either module.
"""

from typing import Dict, List

from PySide6.QtCore import QTimer, Slot

from jellytoast import disk_cache
from jellytoast.async_io import run_async


class _PaginatorMixin:
    """Library-grid fetch/paging state machine, mixed into ``LibraryGrid``.
    Plain-``object`` mixin so the grid keeps a single Qt base (``QWidget``)."""

    # ── Public API ────────────────────────────────────────────────────

    def load_items(self, parent_id: str = "", genre_id: str = "", year: str = ""):
        """Async-fetch items of this grid's ``kind``. Two-phase: if a
        disk cache matches the current scope, render from it instantly
        and verify against the server in the background. On a true
        cold load, fire the regular fetch and persist on success."""
        self._parent_id = parent_id
        self._genre_id = genre_id
        self._year = year
        # Bump the load generation FIRST — before the offline short-circuit
        # below — so any still-in-flight cascade from a prior load_items()
        # (cold fetch, page-by-page auto-paginate, or a background refresh)
        # is superseded on EVERY exit path: its async handlers captured the
        # OLD generation and early-return when they land. Without this a
        # second load_items on the same grid — e.g. _route_home AND
        # _retry_empty_native_views both firing on sign-in — runs two
        # concurrent pagination cascades that double-append every page and
        # over-advance the shared offset (see session_active_dup_albums_bug).
        #
        # Bumping BEFORE the offline branch also closes the sibling race: if
        # an online cascade is still in flight when load_items is re-entered
        # in offline mode, the offline render returns early — and on the old
        # ordering the bump never ran, so those gen-guarded handlers still
        # matched self._load_gen and appended onto the offline grid.
        self._load_gen += 1
        gen = self._load_gen
        self._loading_more = False
        # A silent fill/rebuild cascade still in flight from a PRIOR scope
        # must not resume against — or persist into — the new one. Clear its
        # gate + buffer so this load's own silent fill isn't wedged behind a
        # stale in-flight flag; the old cascade's handlers are gen-guarded
        # and early-return when they land. (The silent cascades carry the
        # same _load_gen token the cold/auto-paginate paths do — without it a
        # mid-fill sort/library switch wrote a cross-scope-poisoned cache.)
        self._silent_fetch_in_flight = False
        self._partial_cache_buffer = []
        # Offline mode short-circuit — render only user-requested
        # downloads for this kind. Pagination, disk cache, refresh
        # round-trip are all bypassed since the source of truth lives
        # in downloads.db and is small.
        from jellytoast import offline as _offline

        if _offline.is_offline_mode():
            self._render_offline_items()
            return

        # Fixed 100-per-page chunks with auto-pagination — the knob
        # to surface "load all" or higher page sizes was dropped from
        # Settings; 100 + auto-paginate keeps cold-start paint snappy
        # and lets the scroll-driven loader chain the rest as the user
        # walks the grid.
        self.PAGE_SIZE = 100
        self._auto_paginate = True
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        scope = {
            "kind": self.kind,
            "parent_id": parent_id,
            "genre_id": genre_id,
            "year": year,
            "sort_by": sort_by,
            "sort_order": self._sort_order,
            # Bumped when item Fields= changed in jellyfin_api.get_items
            # — old caches don't carry the new fields (e.g. Genres
            # added 2026-05-28 for smart-playlist seeding) so a scope
            # bump forces a one-shot re-fetch instead of serving
            # schema-stale items.
            # Bumped to 3 on 2026-05-31: the login double-load bug could
            # persist a doubled/truncated album list to disk; a schema bump
            # discards any such poisoned cache and forces one clean re-fetch.
            "_item_schema": 3,
        }
        self._refresh_scope = scope
        cached = disk_cache.load(self._cache_name, scope)
        if cached:
            # Cache payload is either the legacy bare list (page 1
            # only — written by old versions) or the new envelope dict
            # ``{"items": [...], "complete": bool}`` that stores the
            # full multi-page browse. Handle both for forward + back
            # compat across this rewrite.
            if isinstance(cached, dict):
                cached_items = cached.get("items") or []
                cached_complete = bool(cached.get("complete"))
            else:
                cached_items = cached
                cached_complete = False
            self._cache_was_complete = cached_complete
            # Cache is authoritative for this session — render
            # exactly what we have and mark _complete=True so
            # scroll-triggered pagination doesn't fire and surface
            # "Loading more…" mid-scroll. If the cache is partial,
            # _silent_buffered_fill kicks off in the background and
            # accumulates the rest into a buffer (NOT the rendered
            # model), saving the full payload back to disk before
            # the next launch — so subsequent launches see a
            # complete cache and render everything instantly.
            self._items_loaded.emit(
                {
                    "Items": cached_items,
                    "_complete": True,
                    "_load_gen": gen,
                }
            )
            # Background refresh of page 1 catches mutations since
            # the cache was written. On a signature diff, the
            # _refresh_loaded handler triggers a fresh fetch that
            # re-paginates from scratch.
            run_async(
                self.api.get_items,
                parent_id,
                item_type,
                self.PAGE_SIZE,
                0,
                sort_by,
                self._sort_order,
                True,
                genre_id,
                years=year,
                on_result=lambda resp, g=gen: self._refresh_loaded.emit(
                    {**(resp or {}), "_load_gen": g}
                ),
                on_error=lambda _e: None,
            )
            # Kick off silent buffered fill if the cache is partial
            # AND the user has at least page-1 worth of items (a
            # tiny cache like 8 items isn't worth backfilling since
            # the next launch would just refetch page 1 anyway).
            if not cached_complete and len(cached_items) >= self.PAGE_SIZE:
                self._partial_cache_buffer = []
                QTimer.singleShot(500, lambda g=gen: self._silent_buffered_fill(g))
            return
        self._clear()
        run_async(
            self.api.get_items,
            parent_id,
            item_type,
            self.PAGE_SIZE,
            0,
            sort_by,
            self._sort_order,
            True,
            genre_id,
            years=year,
            on_result=lambda resp, g=gen: self._on_cold_fetch(resp, g),
            on_error=lambda _e, g=gen: self._items_loaded.emit(
                {"Items": [], "_load_gen": g}
            ),
        )

    def _render_offline_items(self):
        """Populate the grid from downloads.db only — the offline-mode
        path. Pagination, disk cache, refresh round-trips, and the
        per-kind parent/genre/year filters are all skipped: downloads
        are small, user-curated, and stored as-is regardless of how
        the user originally browsed to them. ``list_complete_items``
        catches both user-requested roots *and* parents whose state
        rolled up to ``complete`` from a cascaded download (an album
        pulled in by an artist request, etc.)."""
        from jellytoast import offline as _offline

        items = _offline.list_complete_items(self.kind) or []
        items = [it for it in items if it.get("Id")]
        # Artists grid: synthesize artist entries from every
        # downloaded album's AlbumArtists. Downloading an album
        # alone never creates an artist node in the graph, but
        # users expect that album's artist to surface here too —
        # same trick the offline search uses. Real artist nodes
        # win on Id collision so their extra metadata survives.
        if self.kind == "artist":
            by_id = {a["Id"]: a for a in items if a.get("Id")}
            for album in _offline.list_complete_items("album") or []:
                for entry in album.get("AlbumArtists") or []:
                    if not isinstance(entry, dict):
                        continue
                    aid = entry.get("Id")
                    if not aid or aid in by_id:
                        continue
                    by_id[aid] = {
                        "Id": aid,
                        "Name": entry.get("Name") or "",
                        "Type": "MusicArtist",
                    }
            items = list(by_id.values())
        # Disable scroll-pagination + clear stale page state so a
        # later online toggle starts clean.
        self._has_more = False
        self._loading_more = False
        self._auto_paginate = False
        self._completing_partial_cache = False
        self._partial_cache_buffer = []
        self._refresh_scope = {}
        self._items_loaded.emit({"Items": items, "_complete": True})

    def _on_offline_mode_changed(self, _on: bool):
        """Re-render the current scope from the new source.

        Bus connection is QueuedConnection so this lands on the next
        event-loop tick. Hidden grids (the 7 not currently selected in
        the kind switcher) skip the immediate refresh and mark
        themselves dirty — ``showEvent`` consumes the flag the next
        time the user navigates to them. Without this gate, every
        offline-mode toggle would re-run ``load_items`` for all 8
        grids back-to-back, stalling the GUI thread for hundreds of
        ms while the user is staring at Settings."""
        if not _on:
            # Back online: re-fetch covers that were deferred while we
            # were (auto-)offline so a brief connectivity flap doesn't
            # leave tiles permanently blank. Cheap when nothing was
            # deferred; re-issues the visible-window fetches otherwise.
            self._rearm_failed_covers()
        if not self.isVisible():
            self._refresh_after_offline_toggle = True
            return
        self._refresh_after_offline_toggle = False
        self.load_items(self._parent_id, self._genre_id, self._year)

    def _on_cold_fetch(self, resp, gen=None):
        # Drop a result whose load generation has been superseded by a
        # newer load_items() — without this, a second cold fetch (e.g.
        # _retry_empty_native_views firing while the first load is still
        # in flight) runs a parallel pagination cascade that double-appends
        # every page. See session_active_dup_albums_bug memory.
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        # Render first — pagination state lands in _on_items_loaded,
        # which we read below to mark "complete" if the library fits
        # in a single page. Stamp the generation onto the envelope so the
        # _on_items_loaded render + the cascade it kicks stay on this gen.
        if isinstance(resp, dict) and gen is not None:
            resp = {**resp, "_load_gen": gen}
        self._items_loaded.emit(resp)
        if items and self._refresh_scope:
            complete = len(items) < self.PAGE_SIZE
            self._save_cache_async(items, complete)

    def _save_cache_async(self, items: List[Dict], complete: bool):
        """Persist the cache off the GUI thread. A multi-page cache
        (hundreds to thousands of items) serializes to a non-trivial
        JSON blob, and doing it on the GUI thread between page-load
        appends is what makes scroll hitch right after a new page
        lands."""
        scope = dict(self._refresh_scope)
        payload = {"items": list(items), "complete": complete}
        run_async(
            disk_cache.save,
            self._cache_name,
            scope,
            payload,
            on_result=lambda _r: None,
            on_error=lambda _e: None,
        )

    def set_sort(self, sort_by: str, sort_order: str):
        self._sort_by = sort_by or "SortName"
        self._sort_order = "Descending" if sort_order == "descending" else "Ascending"
        self.load_items(self._parent_id, self._genre_id, self._year)

    # ── Pagination ────────────────────────────────────────────────────

    @Slot(int)
    def _maybe_load_more(self, value: int):
        bar = self._view.verticalScrollBar()
        if bar.maximum() <= 0:
            return
        near_bottom = value >= bar.maximum() * self.SCROLL_NEAR_BOTTOM
        if not near_bottom:
            return
        # Drain the silent buffered-fill stash first if anything is
        # waiting. Without this, a partial-cache rehydrate hits the
        # cache boundary at the bottom of the cached portion (e.g.
        # row 100) and the user is stuck there — the buffer-fill is
        # intentionally invisible to keep the cached view steady, but
        # that becomes a UX dead-end at the boundary. Promoting the
        # buffer on scroll-near-bottom keeps the smooth-cache feel
        # while making the rest of the library actually reachable.
        if self._partial_cache_buffer:
            buffered = self._partial_cache_buffer
            self._partial_cache_buffer = []
            # Apply the same client-side article resort every other append path
            # runs (_on_page_loaded / _on_items_loaded), or the drained buffer
            # lands unsorted relative to the already-rendered head.
            buffered = self._resort_items_by_article(buffered)
            base = self._model.rowCount()
            _field = self._alphabet_field_for_sort(self._effective_sort())
            for i, item in enumerate(buffered):
                letter = self._index_letter_for_field(item, _field)
                if letter and letter.isalpha() and letter not in self._letter_to_row:
                    self._letter_to_row[letter] = base + i
            self._loaded_count += len(buffered)
            self._model.append_items(buffered)
            self._load_visible_covers()
            return
        if self._loading_more or not self._has_more:
            return
        self._load_next_page()

    @Slot()
    def _silent_buffered_fill(self, gen=None):
        """Background pagination for a partial cache. Items go into
        a buffer — NOT the rendered model — so the user's view stays
        steady on whatever was in the cache. When the tail is reached
        we save the combined payload back to disk with complete=True,
        and the next launch renders everything instantly.

        Different from the legacy _maybe_load_next_to_complete path:
        that one appended pages to the rendered model, surfacing as
        visible chunked loading. This one is truly invisible — the
        user never sees the additional items in this session, only
        on the next launch."""
        # Superseded by a newer load_items() (sort/library switch) — bail so
        # we don't fetch into, or persist under, the wrong scope.
        if gen is not None and gen != self._load_gen:
            return
        if not self._refresh_scope:
            return
        if self._silent_fetch_in_flight:
            # Another silent fetch (e.g., a refresh-triggered full
            # rebuild) is already populating the buffer; don't
            # interleave a top-up fill.
            return
        self._silent_fetch_in_flight = True
        offset = self._model.rowCount() + len(self._partial_cache_buffer)
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        run_async(
            self.api.get_items,
            self._parent_id,
            item_type,
            self.PAGE_SIZE,
            offset,
            sort_by,
            self._sort_order,
            True,
            self._genre_id,
            years=self._year,
            on_result=lambda resp, g=gen: self._on_silent_buffer_page(resp, g),
            on_error=lambda _e: None,
        )

    def _on_silent_buffer_page(self, resp, gen=None):
        # A page from a superseded generation must not touch the buffer,
        # the in-flight gate, or the (now newer-scope) on-disk cache.
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        if not items:
            # Tail reached. Save full cache as complete=True so the
            # next launch shows everything in one paint.
            if self._refresh_scope:
                full = list(self._model.items()) + self._partial_cache_buffer
                self._save_cache_async(full, True)
            self._partial_cache_buffer = []
            self._silent_fetch_in_flight = False
            return
        self._partial_cache_buffer.extend(items)
        if len(items) < self.PAGE_SIZE:
            # Tail page (short response). Save and stop.
            if self._refresh_scope:
                full = list(self._model.items()) + self._partial_cache_buffer
                self._save_cache_async(full, True)
            self._partial_cache_buffer = []
            self._silent_fetch_in_flight = False
            return
        # Another full page may follow — schedule the next tick.
        # Clear the gate flag so _silent_buffered_fill can re-enter
        # cleanly without being skipped by the in-flight check.
        self._silent_fetch_in_flight = False
        QTimer.singleShot(200, lambda g=gen: self._silent_buffered_fill(g))

    def _maybe_load_next_to_complete(self):
        """Background-pagination tick used to finish filling out a
        partial cache. Gated by `_completing_partial_cache` so we
        stop as soon as the tail is reached, and by `_loading_more`
        so a user-scroll-driven fetch isn't competing with us. The
        footer is intentionally NOT shown for these — silent backfill,
        not user-visible loading status."""
        if not self._completing_partial_cache:
            return
        if self._loading_more or not self._has_more:
            self._completing_partial_cache = False
            return
        self._load_next_page(silent=True)

    def _load_next_page(self, gen=None, silent: bool = False):
        """Fetch the next page. `silent=True` skips the user-visible
        "Loading more…" footer — used for background completion of
        partial caches where the user didn't initiate the fetch.

        `gen` carries the load generation of the cascade that scheduled
        this tick; a superseded tick (a newer load_items() bumped the
        generation) bails so two concurrent cascades can't both paginate
        the same grid. The scroll-driven and partial-cache-completion
        callers pass no gen — they never race a fresh load."""
        if gen is not None and gen != self._load_gen:
            return
        # Hard re-entrancy guard: one page fetch in flight at a time.
        # Without it, two overlapping callers (a stale cascade tick, or a
        # scroll-near-bottom fetch landing on an auto-paginate tick) both
        # read the same _loaded_count offset, fetch the same page, and
        # over-advance the offset — skipping pages and leaving the grid
        # short of the full library.
        if self._loading_more:
            return
        self._loading_more = True
        if not silent:
            self._loading_more_label.setVisible(True)
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        run_async(
            self.api.get_items,
            self._parent_id,
            item_type,
            self.PAGE_SIZE,
            self._loaded_count,
            sort_by,
            self._sort_order,
            True,
            self._genre_id,
            years=self._year,
            on_result=lambda resp, g=gen: self._on_page_loaded(resp, g),
            on_error=lambda _e: self._on_page_error(),
        )

    def _on_page_loaded(self, resp, gen=None):
        items = (resp or {}).get("Items") or []
        # Release the in-flight latch FIRST, then drop a superseded tick.
        # Order matters: returning before clearing _loading_more would
        # wedge this grid's pagination permanently.
        self._loading_more = False
        if gen is not None and gen != self._load_gen:
            return
        if len(items) < self.PAGE_SIZE:
            self._has_more = False
        if not items:
            self._loading_more_label.setVisible(False)
            # Even with no new items, persist the "complete" flag if
            # we've just hit the tail — next launch can skip
            # pagination entirely.
            if self._refresh_scope and not self._has_more:
                self._save_cache_async(self._model.items(), True)
            return
        items = self._resort_items_by_article(items)
        # Augment the alphabet map for the new tail.
        base = self._model.rowCount()
        _field = self._alphabet_field_for_sort(self._effective_sort())
        for i, item in enumerate(items):
            letter = self._index_letter_for_field(item, _field)
            if letter and letter.isalpha() and letter not in self._letter_to_row:
                self._letter_to_row[letter] = base + i
        self._loaded_count += len(items)
        self._model.append_items(items)
        # Hide the footer when there's no more loading queued. In
        # auto-paginate ("load all") mode keep it visible through the
        # 50ms tick gap so the user sees one continuous "Loading more…"
        # indicator rather than a pulsing one.
        if not (self._auto_paginate and self._has_more):
            self._loading_more_label.setVisible(False)
        # Extend the disk cache with the accumulated items so the
        # next launch renders the full library without paging
        # through it again. Off the GUI thread to avoid stutter
        # right after the append (especially for 1000+ item caches).
        if self._refresh_scope:
            self._save_cache_async(
                self._model.items(),
                not self._has_more,
            )
        self._load_visible_covers()
        # Cascade to next page when:
        #   - "load all" auto-paginate mode is on, OR
        #   - we're silently completing a partial cache.
        # Both stop when has_more flips False (tail reached).
        if self._has_more and not self._loading_more:
            if self._auto_paginate:
                QTimer.singleShot(50, lambda g=gen: self._load_next_page(gen=g))
            elif self._completing_partial_cache:
                # Slightly longer delay than auto-paginate so the
                # background backfill doesn't compete with the
                # user's first paint / scroll work.
                QTimer.singleShot(200, self._maybe_load_next_to_complete)
        elif not self._has_more:
            self._completing_partial_cache = False

    def _on_page_error(self):
        self._loading_more = False
        self._loading_more_label.setVisible(False)
        # Stop background backfill on errors — don't spam retries.
        self._completing_partial_cache = False

    @staticmethod
    def _sort_for_kind(sort_by: str, kind: str) -> str:
        if not sort_by:
            return "SortName"
        first_key = sort_by.split(",", 1)[0]
        if kind in ("playlist", "artist") and first_key in ("AlbumArtist", "PremiereDate"):
            return "SortName"
        return sort_by

    def _effective_sort(self) -> str:
        """The sort actually used to FETCH this grid's items — the raw user
        sort adjusted per kind. Artists/playlists can't sort by AlbumArtist
        or PremiereDate, so those fall back to SortName (see _sort_for_kind).
        The alphabet rail and the client-side article resort MUST key off
        this, not the raw self._sort_by: under the "Album artist" sort the
        artist grid fetches by SortName but artist items carry no
        AlbumArtist field, so keying the letter map on the raw sort built an
        empty map and letter jumps silently did nothing."""
        return self._sort_for_kind(self._sort_by, self.kind)

    # ── Async result handlers ─────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        # Drop a render from a superseded load generation (a newer
        # load_items() has since fired) so two concurrent loads can't both
        # paint. gen is absent on the offline-render envelope → unguarded.
        gen = (resp or {}).get("_load_gen")
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        # `_complete` is a private envelope key set by the load_items
        # cache-hit path. When True we know the cache holds the full
        # multi-page browse and there's nothing more to fetch — short-
        # circuit the length-based `_has_more` heuristic and skip the
        # auto-paginate tick below.
        complete = bool((resp or {}).get("_complete"))
        first_load = not self._initial_load_complete
        self._initial_load_complete = True
        items = self._resort_items_by_article(items)
        # Alphabet map — letter → first-matching row index.
        self._letter_to_row = {}
        _field = self._alphabet_field_for_sort(self._effective_sort())
        for i, item in enumerate(items):
            letter = self._index_letter_for_field(item, _field)
            if letter and letter.isalpha() and letter not in self._letter_to_row:
                self._letter_to_row[letter] = i
        self._alphabet.setVisible(self._alphabet_field_for_sort(self._effective_sort()) is not None)
        self._model.set_items(items)
        self._covers_loaded.clear()
        self._cover_retries.clear()
        self._cover_failed.clear()
        self._prefetch_idx = 0
        self._loaded_count = len(items)
        self._has_more = (not complete) and (len(items) >= self.PAGE_SIZE)
        if not items:
            self._show_empty_state()
            return
        # Items arrived — make sure the grid is the visible page.
        self._content_stack.setCurrentIndex(0)
        if self._alphabet.isVisible():
            letter = self._index_letter_for(items[0])
            if letter:
                self._alphabet.set_current_letter(letter)
        # First-load cold-start pre-warm: fire covers for the first N
        # rows directly without waiting for layout. The viewport
        # starts at row 0 with no scroll position to restore, so we
        # can fire the top-of-grid range unconditionally — the cache
        # hits land synchronously into the model + paint events queue
        # against the still-hidden view, so the first paint already
        # has covers. ~16 rows ≈ 4 rows × 4 cols, the typical visible
        # tile-grid surface on a default-size window.
        if first_load:
            for row in range(min(self._INITIAL_PRELOAD_ROWS, len(items))):
                if row in self._covers_loaded:
                    continue
                self._fire_cover_load(row, priority="high")
        # Defer the rest of the visible kickoff to the next event-loop
        # tick so the QListView has a chance to compute its viewport
        # layout from the just-set model. Without this,
        # ``_visible_row_range``'s corner ``indexAt`` probes return
        # invalid (no layout yet), the fallback would widen to
        # ``[0, rowCount)``, and we'd fire a cover load for EVERY
        # item in the library — ~6 s of GUI blocking even when only
        # ~30 rows are actually visible. ``_visible_row_range``
        # returns ``(0, 0)`` when layout isn't ready and
        # ``_load_visible_covers`` retries briefly; the prefetch timer
        # fills in anything not visible at the time of the kickoff.
        QTimer.singleShot(0, self._load_visible_covers)
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()
        if self._auto_paginate and self._has_more and not self._loading_more:
            QTimer.singleShot(50, lambda g=gen: self._load_next_page(gen=g))

    @Slot(object)
    def _on_refresh_loaded(self, resp):
        # The page-1 refresh envelope carries the load generation it was
        # issued under (stamped in load_items); drop it if a newer load has
        # superseded this scope so a stale refresh can't kick a rebuild that
        # persists under the new scope's cache key.
        gen = (resp or {}).get("_load_gen")
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        rendered_first_page = self._model.items()[: self.PAGE_SIZE]
        if self._items_signature(items) == self._items_signature(rendered_first_page):
            # Page 1 is unchanged — but the page-1 signature can't see an
            # album that was added PAST the first page (it sorts into the
            # tail, so the first-100 ID set is identical). For a complete
            # cache, probe the tail for growth before trusting it;
            # otherwise a late-alphabet addition stays invisible behind
            # the stale cache forever (bug 2026-06-02). Partial caches
            # self-heal via the buffered backfill, so skip the probe.
            if self._cache_was_complete:
                self._probe_tail_growth(gen)
            return
        # Library mutated since the cache was written (or Jellyfin
        # tie-break ordering shifted within page 1). Rebuild the
        # cache silently in the background — fetch every page into
        # a buffer, save to disk on completion, do NOT touch the
        # rendered model. The user keeps the smooth view they had,
        # and the next launch picks up the fresh cache. Without
        # this, the prior implementation called _clear() +
        # _on_items_loaded with just page 1, which re-triggered
        # visible chunked pagination on every launch.
        self._partial_cache_buffer = []
        QTimer.singleShot(200, lambda g=gen: self._silent_rebuild_tick(g))

    def _probe_tail_growth(self, gen=None):
        """The page-1 refresh matched, but an album added *past* the
        first page wouldn't change that signature. A *complete* cache of
        N items means the server holds exactly N rows, so a fetch at
        ``offset=N`` should come back empty — ANY row there proves the
        library grew (an insert anywhere bumps the total, so ``offset=N``
        now yields a row) and we silently rebuild. One extra page fetch,
        only when page 1 already matched. (bug 2026-06-02: a late-alphabet
        album hid behind the stale 'complete' cache.)"""
        if gen is not None and gen != self._load_gen:
            return
        cached_count = len(self._model.items())
        if cached_count < self.PAGE_SIZE:
            # Whole library fit in page 1; its signature already covers
            # every row, so there's no tail to probe.
            return
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        run_async(
            self.api.get_items,
            self._parent_id,
            item_type,
            self.PAGE_SIZE,
            cached_count,
            sort_by,
            self._sort_order,
            True,
            self._genre_id,
            years=self._year,
            on_result=lambda resp, g=gen: self._on_tail_probe(resp, g),
            on_error=lambda _e: None,
        )

    def _on_tail_probe(self, resp, gen=None):
        """Tail-probe result. A non-empty page past the cached tail means
        the library grew since the complete cache was written — kick the
        existing silent rebuild so the next launch renders everything.
        Empty ⇒ the cache is still complete; leave it untouched."""
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        if not items:
            return
        self._partial_cache_buffer = []
        QTimer.singleShot(200, lambda g=gen: self._silent_rebuild_tick(g))

    def _silent_rebuild_tick(self, gen=None):
        """Background fetch one page at a time into the rebuild
        buffer. Mirrors _silent_buffered_fill's pattern but starts
        from offset 0 (rebuild) rather than where the cache leaves
        off (top-up)."""
        if gen is not None and gen != self._load_gen:
            return  # superseded by a newer load_items()
        if not self._refresh_scope:
            return
        if self._silent_fetch_in_flight and len(self._partial_cache_buffer) == 0:
            # Concurrent silent fill is using the buffer; wait it
            # out. This is rare — the cache hit path schedules
            # _silent_buffered_fill at 500 ms and the page-1 refresh
            # is usually quicker — but be defensive.
            return
        self._silent_fetch_in_flight = True
        offset = len(self._partial_cache_buffer)
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        run_async(
            self.api.get_items,
            self._parent_id,
            item_type,
            self.PAGE_SIZE,
            offset,
            sort_by,
            self._sort_order,
            True,
            self._genre_id,
            years=self._year,
            on_result=lambda resp, g=gen: self._on_silent_rebuild_page(resp, g),
            on_error=lambda _e: None,
        )

    def _on_silent_rebuild_page(self, resp, gen=None):
        if gen is not None and gen != self._load_gen:
            return  # superseded — don't touch buffer/gate/cache
        items = (resp or {}).get("Items") or []
        if not items:
            if self._refresh_scope:
                self._save_cache_async(self._partial_cache_buffer, True)
            self._partial_cache_buffer = []
            self._silent_fetch_in_flight = False
            return
        self._partial_cache_buffer.extend(items)
        if len(items) < self.PAGE_SIZE:
            if self._refresh_scope:
                self._save_cache_async(self._partial_cache_buffer, True)
            self._partial_cache_buffer = []
            self._silent_fetch_in_flight = False
            return
        self._silent_fetch_in_flight = False
        QTimer.singleShot(200, lambda g=gen: self._silent_rebuild_tick(g))

    @staticmethod
    def _items_signature(items):
        """Order-independent set of item IDs. Was a tuple keyed on
        order, but Jellyfin's tie-break ordering within a single
        sort key (e.g., two albums released the same year) can
        return the same items in a different order across calls,
        which made the cache appear stale on every launch and
        triggered the destructive re-pagination. A frozenset
        compares as equal as long as the set of items matches."""
        return frozenset(it.get("Id", "") for it in items)

    def _clear(self):
        self._model.set_items([])
        self._covers_loaded.clear()
        self._cover_retries.clear()
        self._cover_failed.clear()
        self._prefetch_timer.stop()
        self._prefetch_idx = 0
        self._loaded_count = 0
        self._has_more = False
        self._loading_more = False
        self._completing_partial_cache = False
        self._silent_fetch_in_flight = False
        self._partial_cache_buffer = []
        self._letter_to_row = {}
