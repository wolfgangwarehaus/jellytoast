"""Offline & downloads — explicit "make this available offline" plus
fully-local playback when the server is unreachable.

This package is the self-contained, provider-agnostic, UI-free core
described in ``docs/offline_and_downloads.md`` §9. The split mirrors the
store/index/view separation already used by ``image_cache.py`` /
``disk_cache.py``:

    db.py            SQLite open/migrate; nodes / edges / blobs schema
    index.py         node-graph ops — upsert, link, cascade delete,
                     orphan cleanup, refcount, repair walk
    store.py         blob storage — atomic .part -> rename, relative-path
                     resolution, cover pinning, disk usage
    manager.py       download queue — progress, pause/resume/retry,
                     wifi-only gating, writes nodes.state
    snapshot.py      freeze provider metadata into nodes.metadata_json;
                     re-sync / staleness detection
    connectivity.py  reachable/unreachable state + transition signal
    locations.py     path resolution — the only per-OS file

``__init__`` is the public API surface every other part of the app
imports. Phase 1 (scaffold): ``db`` and ``locations`` are functional;
the rest are honest skeletons that raise ``NotImplementedError`` so the
call sites can be wired incrementally without behaviour changes. See the
phased rollout in the design doc §10.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import db as _db
from . import index as _index
from . import store as _store
from . import snapshot as _snapshot
from . import manager as _manager
from . import connectivity as _connectivity
from . import locations as _locations
from . import library_sync as _library_sync


# ── Lifecycle ───────────────────────────────────────────────────────────────


def init() -> None:
    """Open + migrate ``downloads.db``, ensure the blob store directory
    exists, and bring the connectivity tracker up so persisted
    offline-mode is restored + a boot ``offline_mode_changed`` lands
    for subscribers. Safe to call more than once (idempotent). Call
    once at app startup, after settings are available.

    Also re-queues any downloads that were mid-flight when the app
    last closed — ``manager.resume_pending`` walks the index for
    nodes in state ``pending``/``downloading`` and pushes their leaf
    tracks back into the queue. The ``.part`` files left from the
    previous session are safe: ``commit_blob`` only writes the
    ``blobs`` row after the atomic rename, so a half-downloaded file
    is self-evidently incomplete and the next attempt overwrites it
    from byte zero."""
    _db.connect()
    _locations.downloads_dir()  # mkdir side-effect
    _connectivity.init()
    _library_sync.init()
    _manager.resume_pending()
    # Re-prime the expected-total counter from QSettings so the
    # aggregate display reads "Downloading X of Y" with the same
    # stable Y as before the restart. Cleared on the next drain-edge.
    try:
        from modules.settings import get_settings
        s = get_settings()
        if s.library_download_in_progress and s.library_download_expected_total > 0:
            _manager.set_session_expected_total(s.library_download_expected_total)
    except Exception:
        pass


def resume_pending() -> int:
    """Re-queue any downloads that were mid-flight when the app last
    closed. Called automatically from ``init`` — exposed here so a
    future "Retry stuck downloads" button can re-trigger it without
    a full restart. Returns the count actually re-queued."""
    return _manager.resume_pending()


def note_request_success() -> None:
    """Provider call sites call this on a successful API response so
    the connectivity tracker can lift "unreachable" state + clear
    auto-offline mode."""
    _connectivity.note_success()


def note_request_failure() -> None:
    """Provider call sites call this on a network-class exception
    (timeout, connection refused, DNS failure). Once a small string of
    failures crosses the threshold, ``connectivity_changed(False)``
    fires + auto-offline mode flips on if enabled."""
    _connectivity.note_network_failure()


def note_auth_failure() -> None:
    """Provider call sites call this on a definitive auth-reject (HTTP
    401/403 for Jellyfin, Subsonic error 40 for Subsonic). Past the
    threshold ``auth_failed`` fires so the UI can drop to LoginView."""
    _connectivity.note_auth_failure()


def note_auth_success() -> None:
    """Provider call sites call this on a successful auth-bearing
    response. Resets the auth-failure counter so a one-off transient
    rejection (Navidrome boot-ping quirk, brief server hiccup) doesn't
    accumulate across long sessions."""
    _connectivity.note_auth_success()


# ── Query ───────────────────────────────────────────────────────────────────


def is_downloaded(item_id: str) -> bool:
    """True if ``item_id`` has a complete local blob for the current
    server identity. Cheap — a single indexed lookup; safe to call
    from paint / context-menu-build paths."""
    return _index.is_complete(item_id)


def local_blob(item_id: str) -> "Optional[_store.Blob]":
    """Resolved local blob for ``item_id``, or ``None`` if not
    downloaded. The returned path is absolute (relative path in the DB
    resolved against the runtime base dir). Used by
    ``QueueManager._build_now_playing`` to prefer the local copy."""
    return _store.resolve(item_id)


def list_downloads(kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """Every user-requested download node (``requested = 1``),
    newest first, optionally filtered to one ``kind``. Children pulled
    in via cascade are excluded — this is the "things I asked for"
    list the downloads screen renders."""
    return _index.list_requested(kind)


def storage_usage() -> Dict[str, int]:
    """Bytes on disk, broken out by node kind plus a ``total``. Backs
    the settings "Storage used" read-out."""
    return _store.usage()


def item_size(item_id: str) -> int:
    """On-disk bytes for one downloaded item and everything below it —
    a track's blob, or the sum across an album / playlist / artist.
    Backs the per-row size in the downloads screen."""
    return _store.subtree_bytes(item_id)


# ── Snapshot accessors ─────────────────────────────────────────────────────


def get_snapshot(item_id: str) -> "Optional[Dict[str, Any]]":
    """Frozen metadata dict for a downloaded node, or ``None`` if the
    item isn't in the offline graph. Offline views read this instead of
    the live provider when the server is unreachable."""
    node = _index.get_node(item_id)
    if node is None:
        return None
    try:
        meta = json.loads(node.get("metadata_json") or "{}")
    except (ValueError, TypeError):
        meta = {}
    return meta or None


def child_snapshots(item_id: str, kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """Frozen metadata dicts for the direct children of ``item_id``,
    optionally filtered to one ``kind`` (e.g. ``album`` to get an
    artist's albums, ``track`` for an album's tracks). Empty list if
    the parent has no children in the graph."""
    out: List[Dict[str, Any]] = []
    for child_item_id in _index.children(item_id):
        node = _index.get_node(child_item_id)
        if node is None:
            continue
        if kind and node.get("kind") != kind:
            continue
        try:
            meta = json.loads(node.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        if meta:
            out.append(meta)
    return out


def list_complete_items(kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """Every node in state ``complete`` for the current server identity,
    optionally filtered to one ``kind``. Returns the frozen metadata
    dicts (not the raw node rows) so callers can treat them like live
    provider items. Used by offline views as a "what's available" pool
    independent of the user-requested set."""
    ident = _index.server_identity()
    sql = "SELECT * FROM nodes WHERE state = 'complete' AND id LIKE ? "
    params: tuple = (f"{ident}:%",)
    if kind:
        sql += "AND kind = ? "
        params += (kind,)
    out: List[Dict[str, Any]] = []
    for r in _db.query(sql, params):
        row = dict(r)
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        if meta:
            out.append(meta)
    return out


# ── Mutate ──────────────────────────────────────────────────────────────────


def download(item: Dict[str, Any]) -> None:
    """Mark ``item`` (an album / playlist / artist / track dict) for
    download: snapshot its metadata, expand children into the node
    graph, enqueue the audio blobs. Returns immediately — progress is
    reported via the ``download_progress`` bus signal."""
    _manager.enqueue(item)


def clear_all() -> int:
    """Remove every user-requested download. Returns the count
    actually removed. Iterates ``list_downloads()`` and calls
    ``remove`` on each; cascade rows (album/artist/playlist) drop
    their child tracks via the existing manager teardown.

    Destructive — callers should confirm with the user first. The
    ``download_progress(item_id, "removed", 0.0)`` emits drive the
    DownloadsLibraryView row teardowns."""
    items = list_downloads()
    count = 0
    for node in items:
        item_id = node.get("item_id")
        if not item_id:
            continue
        try:
            _manager.remove(item_id)
            count += 1
        except Exception:
            # One bad remove shouldn't abort the rest of the sweep.
            continue
    return count


def remove(item_id: str) -> None:
    """Delete a download and cascade: walk ``edges``, drop the node and
    any child orphaned by its removal (a track still in another
    playlist survives). Blob files are unlinked off the GUI thread."""
    _manager.remove(item_id)


def repair() -> Dict[str, int]:
    """Walk every ``complete`` node, re-sync each against the provider,
    and return a summary::

        {
            "checked":             nodes inspected,
            "marked_stale":        blobs now flagged stale,
            "deleted_server_side": items the server no longer knows,
            "errors":              provider fetches that raised,
        }

    Drives the future Settings → Downloads → "Repair downloads" button.
    The blob files themselves are **never** touched here — surfacing
    drift is the goal, not garbage collection; the user gets the final
    call. Provider round-trips happen serially on the calling thread
    (the UI is expected to run this off the GUI thread; ``async_io``
    wraps it on the wired surface)."""
    summary = {
        "checked": 0,
        "marked_stale": 0,
        "deleted_server_side": 0,
        "errors": 0,
    }
    # list_complete returns rows by kind; walk every kind so artists /
    # albums / playlists with their own metadata get re-checked too.
    seen: set = set()
    for kind in ("track", "album", "artist", "playlist"):
        for row in _index.list_complete(kind):
            item_id = row.get("item_id")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            summary["checked"] += 1
            result = _snapshot.resync(item_id)
            if result.get("error"):
                summary["errors"] += 1
                continue
            if result.get("marked_stale"):
                summary["marked_stale"] += 1
            if result.get("deleted_server_side"):
                summary["deleted_server_side"] += 1
    return summary


# ── Offline mode ────────────────────────────────────────────────────────────


def set_offline_mode(enabled: bool) -> None:
    """Toggle offline mode: views read from ``downloads.db`` only and
    non-downloaded items are hidden / disabled. Emits
    ``offline_mode_changed`` on the bus."""
    _connectivity.set_offline_mode(enabled)


def is_offline_mode() -> bool:
    """True if offline mode is active (explicit toggle or auto-offline
    triggered by an unreachable server)."""
    return _connectivity.is_offline_mode()


def set_wifi_only(value: bool) -> None:
    """Toggle the "Only download on Wi-Fi" gate. Persists across
    restart and emits ``downloads_wifi_only_changed`` on the bus.
    Pairs with ``mark_metered`` — the gate only actually blocks
    dispatch when both are True."""
    _manager.set_wifi_only(value)


def is_wifi_only() -> bool:
    """True if the user has opted into the Wi-Fi-only download gate."""
    return _manager.is_wifi_only()


def mark_metered(value: bool) -> None:
    """Stub for the future network-auto-detect layer to flag the current
    connection as metered / cellular. Transient (not persisted). When
    paired with ``is_wifi_only() == True``, blocks ``_dispatch`` from
    popping new jobs."""
    _manager.mark_metered(value)


def is_on_metered() -> bool:
    """True if the metered-network stub flag is currently set."""
    return _manager.is_on_metered()


# ── Download queue control ─────────────────────────────────────────────────


def pause() -> None:
    """Pause the download queue. In-flight blobs run to completion; the
    queue idles. Persists across restart and emits
    ``download_queue_paused`` on the bus."""
    _manager.pause()


def resume() -> None:
    """Resume the download queue. Kicks the dispatcher so waiting jobs
    start. Persists across restart and emits ``download_queue_resumed``."""
    _manager.resume()


def is_paused() -> bool:
    """True when the download queue is paused."""
    return _manager.is_paused()


def get_queue_stats() -> "tuple[int, int, float, float]":
    """One-shot snapshot of the download-queue aggregate stats:
    ``(active, total_session, speed_bps, eta_seconds)``. Same payload
    shape as the ``download_queue_stats`` bus signal — intended for
    callers (the future DownloadsView aggregate block) that want to
    prime UI on construction without waiting for the next 1 Hz tick.
    Cheap: a pure read of in-memory counters."""
    return _manager.get_queue_stats()


def resync(item_id: str) -> Dict[str, Any]:
    """Re-fetch ``item_id`` from the provider and reconcile the stored
    snapshot. Provider round-trip — call off the GUI thread (use
    ``modules.async_io.run_async``). See ``snapshot.resync`` for the
    result-dict shape."""
    return _snapshot.resync(item_id)


def sync_library(on_progress=None) -> "tuple[int, int]":
    """Walk every album in the active provider's library and enqueue
    the ones that aren't already downloaded. Returns ``(total_seen,
    newly_enqueued)``. Provider round-trip — invoke off the GUI thread
    via ``modules.async_io.run_async``."""
    return _library_sync.sync_library(on_progress)


def start_periodic_library_sync() -> None:
    """Start the 6-hour re-sync timer that re-runs ``sync_library`` to
    pull in newly-added albums. Idempotent."""
    _library_sync.start_periodic_sync()


def stop_periodic_library_sync() -> None:
    """Stop the 6-hour re-sync timer."""
    _library_sync.stop_periodic_sync()


def is_server_reachable() -> bool:
    """Best-effort: whether the media server is currently reachable,
    tracked from API-call outcomes. Used by the playback path to fall
    back to a downloaded copy when the server is down."""
    return _connectivity.is_server_reachable()


def active_host_label() -> str:
    """Label of the currently active host. Empty when the primary
    ``server_url`` is in use; a non-empty string ("Tailscale",
    "LAN", …) when a multi-server fallback has swapped to an
    alternate. Useful for a status chip / toast."""
    return _connectivity.active_host_label()


def probe_now(on_done=None) -> None:
    """Fire an immediate recovery probe — used by the offline chip's
    "try reconnecting" click and by the periodic auto-probe loop.
    First reachable host becomes active and lifts auto-offline.
    ``on_done(recovered: bool)`` fires on the GUI thread once the
    probe completes so the caller can exit any pending UI state."""
    _connectivity.probe_now(on_done)


__all__ = [
    "init",
    "is_downloaded",
    "local_blob",
    "list_downloads",
    "list_complete_items",
    "get_snapshot",
    "child_snapshots",
    "storage_usage",
    "item_size",
    "download",
    "remove",
    "repair",
    "set_offline_mode",
    "is_offline_mode",
    "set_wifi_only",
    "is_wifi_only",
    "mark_metered",
    "is_on_metered",
    "pause",
    "resume",
    "is_paused",
    "get_queue_stats",
    "resync",
    "sync_library",
    "start_periodic_library_sync",
    "stop_periodic_library_sync",
    "clear_all",
    "resume_pending",
    "is_server_reachable",
    "active_host_label",
    "probe_now",
    "note_request_success",
    "note_request_failure",
    "note_auth_failure",
    "note_auth_success",
]
