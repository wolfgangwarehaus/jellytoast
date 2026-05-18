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

import time
from typing import Any, Callable, Optional, Tuple

_PAGE_SIZE = 100
_SYNC_INTERVAL_MS = 6 * 60 * 60 * 1000  # 6 hours

# Phase-2 backpressure: don't let the album loop queue more than this
# many ``_plan`` jobs concurrently. Without it, a library with N albums
# would land N FIFO planning tasks on the shared QThreadPool before the
# first ``_download_track`` job got a slot, leaving the aggregate stuck
# at "0 of N" for minutes while plannings drained. 12 is roomy enough
# that planning never stalls (with pool=8 it stays saturated) but tight
# enough that dispatched track downloads aren't queued behind hundreds
# of pending plannings.
_PLAN_INFLIGHT_CAP = 12

# Backpressure poll interval — how long phase 2 sleeps between checks
# when the planning cap is full. Short enough to feel responsive,
# long enough that polling overhead is negligible.
_PLAN_POLL_S = 0.05

_sync_timer: Any = None  # QTimer

# Cooperative cancel flag — flipped to True by ``cancel_walk()``; the
# phase-2 loop polls it between iterations and bails. Set back to False
# at the top of every ``sync_library`` call so the next walk starts
# clean.
_cancel_requested: bool = False


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

    global _cancel_requested
    _cancel_requested = False

    provider = get_provider()
    all_albums: list = []
    start_index = 0

    # Phase 1: enumerate.
    while True:
        if _cancel_requested:
            print("[jellytoast] library walk cancelled (phase 1)", flush=True)
            return len(all_albums), 0
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
    print(
        f"[jellytoast] library walk phase 1 complete — {len(all_albums)} albums",
        flush=True,
    )

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
        # Persist so a paused mid-walk session survives an app restart
        # with the same stable "of Y" total. DownloadsView's drain-edge
        # handler clears this alongside ``library_download_in_progress``.
        try:
            from modules.settings import get_settings
            get_settings().library_download_expected_total = expected_tracks
        except Exception:
            pass

    # Phase 2: enqueue. Backpressured against
    # ``manager._planning_in_flight`` so the planning queue never grows
    # tall enough to shut out ``_download_track`` jobs (which compete
    # for the same FIFO pool slots). Each album's ``download()`` call
    # bumps the counter; planning's success/error callback decrements
    # it. Phase 2 sleeps a short interval whenever the cap is hit.
    enqueued = 0
    for album in all_albums:
        if _cancel_requested:
            print(
                f"[jellytoast] library walk cancelled (phase 2 at {enqueued})",
                flush=True,
            )
            break
        item_id = album.get("Id")
        if not item_id:
            continue
        if _offline_pkg.is_downloaded(item_id):
            continue
        while getattr(_mgr, "_planning_in_flight", 0) >= _PLAN_INFLIGHT_CAP:
            if _cancel_requested:
                break
            time.sleep(_PLAN_POLL_S)
        if _cancel_requested:
            break
        try:
            _offline_pkg.download(album)
            enqueued += 1
        except Exception:
            continue

    print(
        f"[jellytoast] library walk phase 2 complete — {enqueued} albums enqueued",
        flush=True,
    )

    if on_progress is not None:
        try:
            on_progress(len(all_albums), enqueued)
        except Exception:
            pass

    return len(all_albums), enqueued


def cancel_walk() -> None:
    """Cooperatively cancel an in-flight ``sync_library`` call. The
    next iteration of phase 1 or phase 2 sees the flag and returns
    early. Already-dispatched track downloads keep running — pair with
    ``offline.pause()`` to stop those too."""
    global _cancel_requested
    _cancel_requested = True


def is_walk_cancelled() -> bool:
    """Test/observability hook — current state of the cancel flag."""
    return _cancel_requested


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
    global _sync_timer, _cancel_requested
    if _sync_timer is not None:
        try:
            _sync_timer.stop()
        except Exception:
            pass
    _sync_timer = None
    _cancel_requested = False
