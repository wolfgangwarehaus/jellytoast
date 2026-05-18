"""Library sync — bulk-download every album in the user's library, with
optional periodic re-sync to pull new albums as they appear on the
server.

This isn't a new persistence layer; it's a thin orchestrator over the
existing pieces:

- ``provider.get_items(item_type="MusicAlbum", ...)`` paginates the
  whole library (~100 albums per page).
- For each album, ``offline.download(album)`` is the existing
  per-album entrypoint; it snapshots metadata, expands children,
  enqueues the audio blobs. Already-complete items are skipped by
  the manager, so re-running is idempotent.

The periodic timer is a single GUI-thread ``QTimer`` started/stopped by
the ``library_sync_enabled`` setting. v1 cadence: every 6 hours. The
timer doesn't fire at app start — call ``sync_library()`` once
explicitly if you want an immediate pass.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

_PAGE_SIZE = 100
_SYNC_INTERVAL_MS = 6 * 60 * 60 * 1000  # 6 hours

_sync_timer: Any = None  # QTimer


def sync_library(
    on_progress: "Optional[Callable[[int, int], None]]" = None,
) -> Tuple[int, int]:
    """Walk every album in the active provider's library and enqueue
    the ones that aren't already downloaded. Returns
    ``(total_albums, newly_enqueued)``.

    Two-phase so the aggregate progress display reads a stable total
    from the start instead of climbing as new tracks dispatch:

    1. **Enumerate.** Paginate ``provider.get_items(item_type=
       "MusicAlbum")``, collect album dicts in memory, and sum
       ``ChildCount`` (Jellyfin) / normalised ``ChildCount``
       (Subsonic — ``songCount`` adapted at the provider) for the
       total track count. Register that count on the manager so the
       stats signal clamps "X of Y" to it.
    2. **Enqueue.** Iterate the cached list, calling
       ``offline.download`` for each album not already complete.
       Idempotent — already-downloaded items skip on the manager
       side.

    ``on_progress(seen, enqueued)`` fires after each enumeration page
    so the caller can update UI; safe to skip. Provider round-trips
    happen on the calling thread — invoke through
    ``modules.async_io.run_async`` so the GUI doesn't stall on large
    libraries."""
    from modules.providers import get_provider
    from modules import offline as _offline_pkg
    from . import manager as _mgr

    provider = get_provider()
    all_albums: list = []
    start_index = 0

    # Phase 1: enumerate.
    while True:
        response = provider.get_items(
            item_type="MusicAlbum",
            limit=_PAGE_SIZE,
            start_index=start_index,
        )
        items = response.get("Items") or []
        if not items:
            break
        all_albums.extend(items)
        if on_progress is not None:
            try:
                on_progress(len(all_albums), 0)
            except Exception:
                pass
        if len(items) < _PAGE_SIZE:
            break
        start_index += _PAGE_SIZE

    # Sum the per-album ``ChildCount`` to get a stable total-tracks
    # number for the aggregate. Both providers normalise to this key.
    # Missing / zero-count albums simply don't contribute.
    expected_tracks = 0
    for album in all_albums:
        try:
            expected_tracks += int(album.get("ChildCount") or 0)
        except (TypeError, ValueError):
            continue
    if expected_tracks > 0:
        _mgr.set_session_expected_total(expected_tracks)

    # Phase 2: enqueue.
    enqueued = 0
    for album in all_albums:
        item_id = album.get("Id")
        if not item_id:
            continue
        if _offline_pkg.is_downloaded(item_id):
            continue
        try:
            _offline_pkg.download(album)
            enqueued += 1
        except Exception:
            continue

    if on_progress is not None:
        try:
            on_progress(len(all_albums), enqueued)
        except Exception:
            pass

    return len(all_albums), enqueued


def start_periodic_sync() -> None:
    """Start the 6-hour re-sync timer. Idempotent — safe to call from
    a settings-toggle handler that doesn't know the timer's state."""
    global _sync_timer

    try:
        from PySide6.QtCore import QTimer
    except Exception:
        # Headless / Qt-import failure (mainly tests) — no-op.
        return

    if _sync_timer is not None and _sync_timer.isActive():
        return

    if _sync_timer is None:
        _sync_timer = QTimer()
        _sync_timer.setInterval(_SYNC_INTERVAL_MS)
        _sync_timer.timeout.connect(_on_tick)
    _sync_timer.start()


def stop_periodic_sync() -> None:
    """Stop the re-sync timer if running."""
    global _sync_timer
    if _sync_timer is not None and _sync_timer.isActive():
        _sync_timer.stop()


def is_periodic_sync_running() -> bool:
    return _sync_timer is not None and _sync_timer.isActive()


def init() -> None:
    """Boot-time hook: start the periodic timer iff the setting is on.
    Called from ``offline.init`` so the app respects the persisted
    choice without callers needing to wire it up."""
    try:
        from modules.settings import get_settings
    except Exception:
        return
    if get_settings().library_sync_enabled:
        start_periodic_sync()


def _on_tick() -> None:
    """Periodic tick — runs the sync off the GUI thread. The actual
    enqueue stays GUI-thread-safe via the existing ``offline.download``
    path (which uses the queued bus signal pattern)."""
    try:
        from modules.async_io import run_async
    except Exception:
        return
    run_async(sync_library, on_result=lambda _r: None, on_error=lambda _e: None)


def _reset_for_tests() -> None:
    """Test hook — stop + drop the timer so per-test state stays clean."""
    global _sync_timer
    if _sync_timer is not None:
        try:
            _sync_timer.stop()
        except Exception:
            pass
    _sync_timer = None
