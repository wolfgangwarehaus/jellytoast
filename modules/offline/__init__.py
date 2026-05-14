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

from typing import Any, Dict, List, Optional

from . import db as _db
from . import index as _index
from . import store as _store
from . import snapshot as _snapshot
from . import manager as _manager
from . import connectivity as _connectivity
from . import locations as _locations


# ── Lifecycle ───────────────────────────────────────────────────────────────

def init() -> None:
    """Open + migrate ``downloads.db`` and ensure the blob store
    directory exists. Safe to call more than once (idempotent). Call
    once at app startup, after settings are available."""
    _db.connect()
    _locations.downloads_dir()  # mkdir side-effect


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


# ── Mutate ──────────────────────────────────────────────────────────────────

def download(item: Dict[str, Any]) -> None:
    """Mark ``item`` (an album / playlist / artist / track dict) for
    download: snapshot its metadata, expand children into the node
    graph, enqueue the audio blobs. Returns immediately — progress is
    reported via the ``download_progress`` bus signal."""
    _manager.enqueue(item)

def remove(item_id: str) -> None:
    """Delete a download and cascade: walk ``edges``, drop the node and
    any child orphaned by its removal (a track still in another
    playlist survives). Blob files are unlinked off the GUI thread."""
    _manager.remove(item_id)

def repair() -> Dict[str, int]:
    """Reconcile the index against disk: drop ``blobs`` rows with no
    file, re-link orphans, recompute sizes. Returns a summary dict.
    Cheap insurance — the node graph is designed so this is a walk."""
    return _index.repair()


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


__all__ = [
    "init",
    "is_downloaded", "local_blob", "list_downloads", "storage_usage",
    "download", "remove", "repair",
    "set_offline_mode", "is_offline_mode",
]
