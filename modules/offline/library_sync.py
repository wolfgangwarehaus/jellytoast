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
    ``(total_seen, newly_enqueued)``.

    ``on_progress(seen, enqueued)`` fires after each page so the caller
    can update UI; safe to skip. Provider round-trips happen on the
    calling thread — invoke through ``modules.async_io.run_async``
    so the GUI doesn't stall on large libraries."""
    from modules.providers import get_provider
    from modules import offline as _offline_pkg

    provider = get_provider()
    total_seen = 0
    enqueued = 0
    start_index = 0

    while True:
        response = provider.get_items(
            item_type="MusicAlbum",
            limit=_PAGE_SIZE,
            start_index=start_index,
        )
        items = response.get("Items") or []
        if not items:
            break

        for album in items:
            total_seen += 1
            item_id = album.get("Id")
            if not item_id:
                continue
            if _offline_pkg.is_downloaded(item_id):
                continue
            try:
                _offline_pkg.download(album)
                enqueued += 1
            except Exception:
                # Manager will surface failures via ``download_progress``;
                # one bad enqueue shouldn't abort the whole walk.
                continue

        if on_progress is not None:
            try:
                on_progress(total_seen, enqueued)
            except Exception:
                pass

        if len(items) < _PAGE_SIZE:
            break
        start_index += _PAGE_SIZE

    return total_seen, enqueued


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
