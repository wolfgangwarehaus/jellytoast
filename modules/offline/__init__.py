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


# ── Lifecycle ───────────────────────────────────────────────────────────────

def init() -> None:
    """Open + migrate ``downloads.db``, ensure the blob store directory
    exists, and bring the connectivity tracker up so persisted
    offline-mode is restored + a boot ``offline_mode_changed`` lands
    for subscribers. Safe to call more than once (idempotent). Call
    once at app startup, after settings are available."""
    _db.connect()
    _locations.downloads_dir()  # mkdir side-effect
    _connectivity.init()


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

def list_complete_items(kind: str) -> List[Dict[str, Any]]:
    """Every complete (``state = 'complete'``) node of ``kind`` under
    the current server identity, including those pulled in by a parent
    download. Backs the Songs offline view, where the cascading-in
    tracks of an album download are exactly what users want to see."""
    return _index.list_complete(kind)

def get_snapshot(item_id: str) -> "Optional[Dict[str, Any]]":
    """Frozen provider metadata for a downloaded item, or ``None`` if
    no node exists. Same shape as the original provider item dict —
    detail surfaces (artist page, album view) can render from this
    offline without a provider round-trip."""
    return _index.get_snapshot(item_id)

def child_snapshots(item_id: str,
                    kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """Frozen metadata for every direct child of ``item_id``,
    optionally filtered to one ``kind``. Artist → albums, album →
    tracks, playlist → tracks. Returns ``[]`` when the parent has no
    edges yet (e.g. only the parent was downloaded)."""
    return _index.child_snapshots(item_id, kind)

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


def child_snapshots(item_id: str, kind: "Optional[str]" = None) \
        -> List[Dict[str, Any]]:
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

def is_server_reachable() -> bool:
    """Best-effort: whether the media server is currently reachable,
    tracked from API-call outcomes. Used by the playback path to fall
    back to a downloaded copy when the server is down."""
    return _connectivity.is_server_reachable()


__all__ = [
    "init",
    "is_downloaded", "local_blob", "list_downloads", "list_complete_items",
    "get_snapshot", "child_snapshots", "storage_usage", "item_size",
    "download", "remove", "repair",
    "set_offline_mode", "is_offline_mode", "is_server_reachable",
    "note_request_success", "note_request_failure",
]
