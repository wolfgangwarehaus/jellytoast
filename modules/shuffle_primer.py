"""Library-shuffle / random-queue priming for the main window.

The "shuffle my library" button glue, extracted from ``jellytoast.py``: a
re-entrancy-guarded random fetch, a one-deep pre-fetched cache for instant
re-clicks, and the queue install.

``_ShufflePrimerMixin`` is mixed into ``JellytoastWindow`` — not standalone.
Its methods reference window state (``self._shuffle_in_flight``,
``self._random_queue_cache``, ``self.provider``) and call the sibling
LibrarySelection mixin's ``self._resolve_library_id``; all resolve on the
combined instance.

NB ``run_async`` is imported here; a future test stubbing the shuffle fetch
must patch ``shuffle_primer.run_async``.
"""

import logging

from modules.async_io import run_async
from modules.player_state import QueueContext, QueueKind
from modules.settings import get_settings

logger = logging.getLogger("jellytoast")


class _ShufflePrimerMixin:
    """Library-shuffle + random-queue priming, mixed into ``JellytoastWindow``.
    Plain-``object`` mixin (single Qt base on the window)."""

    def _library_shuffle(self):
        # Re-entry guard so a rapid double-click of the shuffle button
        # doesn't kick off two parallel REST fetches and two competing
        # queue installs.
        if self._shuffle_in_flight:
            logger.info("library shuffle skipped — already in flight")
            return
        self._shuffle_in_flight = True

        # Fast path: a pre-fetched random queue is sitting in the cache.
        # Emit it immediately, then refill the cache in the background.
        if self._random_queue_cache:
            items = self._random_queue_cache
            self._random_queue_cache = []
            self._install_shuffle_queue(items, "library shuffle (cached)")
            self._prime_random_queue_async()
            self._shuffle_in_flight = False
            return

        lib_id = self._resolve_library_id("music")
        if not lib_id:
            logger.warning("no music library resolved; skipping library shuffle")
            self._shuffle_in_flight = False
            return
        # Cache miss — fetch on the shared QThreadPool so the GUI
        # doesn't freeze while the random items load. Limit comes from
        # Settings (default 100) — smaller queues commit faster after
        # a drag-reorder since _populate_rows rebuilds every row.
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items,
            lib_id,
            limit=shuffle_n,
            on_result=self._on_library_shuffle_loaded,
            on_error=self._on_library_shuffle_error,
        )

    def _on_library_shuffle_loaded(self, items):
        try:
            if not items:
                logger.warning("library shuffle: API returned no tracks")
                return
            self._install_shuffle_queue(items, "library shuffle")
            # Prime the cache for the next click while we're already
            # warmed up (lib_id resolved, API connection live).
            self._prime_random_queue_async()
        finally:
            self._shuffle_in_flight = False

    def _on_library_shuffle_error(self, e):
        logger.warning("library shuffle fetch failed: %s", e)
        self._shuffle_in_flight = False

    def _install_shuffle_queue(self, items: list, source_label: str):
        """Install a randomly-ordered library queue and start it. The
        log line gives shuffle diagnostics (item count, unique album
        count) so the per-intent debugging picture stays readable when
        JT_SHUFFLE_DEBUG is on."""
        from modules.player_state import PlayerBus

        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
        logger.info(
            "queue set via %s: %s items, %s unique albums, start=0",
            source_label,
            len(items),
            len(unique_albums),
        )
        ctx = QueueContext(kind=QueueKind.SHUFFLE, source_label="Library shuffle")
        PlayerBus.get().queue_play_now.emit(items, 0, ctx)

    def _prime_random_queue_async(self):
        """Refresh the pre-fetched random queue in the background.
        No-ops if a cache already exists or no music library is known."""
        if self._random_queue_cache:
            return
        lib_id = self._resolve_library_id("music")
        if not lib_id:
            return
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items,
            lib_id,
            limit=shuffle_n,
            on_result=self._on_prime_random_queue_loaded,
            on_error=lambda e: logger.warning(
                "prime random queue failed: %s", e
            ),
        )

    def _on_prime_random_queue_loaded(self, items):
        if items:
            self._random_queue_cache = items
            logger.info(
                "random queue cache primed: %s items", len(items)
            )
