"""Metadata snapshots — the authoritative offline record.

When the user downloads something, the audio bytes are only one of three
legs (design doc §2). This module owns the second: freezing the provider
item dicts — album/artist/playlist info plus every track's title, artist,
duration, track/disc number, IDs — into ``nodes.metadata_json``.

That snapshot is **authoritative and point-in-time**. It is deliberately
*not* ``disk_cache.py``'s browse cache: the browse cache is a convenience
layer that can be scoped away or cleared at any moment, whereas an
offline-mode view must still render a downloaded album a year later. The
cost of that independence is drift — a track renamed server-side won't
show until a manual re-sync (v1; background favourite-sync is a later
follow-on, the way Finamp does it).

Phase 1: skeleton. The provider methods it will call already exist
(``get_item`` / ``get_album_tracks`` / ``get_artist_albums`` /
``get_playlist_items`` — design doc §8), so Phase 2 is purely a caller
that freezes their results; no new provider method is needed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Provider ``Type`` string -> our generic node ``kind``. ``kind`` is
# data, not schema (design doc §5.1), so this map is the only place
# the provider's type vocabulary is interpreted.
_TYPE_TO_KIND = {
    "audio": "track",
    "musicalbum": "album",
    "musicartist": "artist",
    "playlist": "playlist",
}


def kind_of(item: Dict[str, Any]) -> str:
    """Generic node ``kind`` for a provider item dict. Falls back to
    ``track`` for an unrecognised non-folder type and ``album`` for an
    unrecognised folder — the conservative guesses (a leaf is a track,
    a container is album-shaped)."""
    t = (item.get("Type") or "").strip().lower()
    if t in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[t]
    return "album" if item.get("IsFolder") else "track"


def freeze(item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Resolve ``item`` into its authoritative snapshot: the parent's
    own metadata dict plus the list of child item dicts (tracks for an
    album/playlist, albums for an artist; a track returns itself and an
    empty child list). Returns ``(parent_meta, children)`` ready for
    ``index.upsert_node`` + ``index.link``.

    The **track** case: the item dict handed in is already the
    complete, authoritative metadata for a single track — title,
    artists, album, duration, track/disc no., IDs — so the snapshot is
    a copy of it, no provider round-trip. (A Phase 6 re-sync is what
    refreshes a track snapshot against server edits.)

    The **album / playlist / artist** cases do a single provider
    round-trip for the direct children — tracks for an album/playlist,
    albums for an artist. The caller (``manager._plan``) recurses into
    the returned children, so an artist expands artist -> albums ->
    tracks across nested ``freeze`` calls. **Always call this off the
    GUI thread** — the round-trip blocks.

    Unknown kinds return ``(item, [])`` rather than raising: a node we
    can't expand is still a valid leaf to snapshot."""
    kind = kind_of(item)
    if kind == "track":
        return dict(item), []

    from jellytoast.providers import get_provider

    api = get_provider()
    item_id = item.get("Id", "")
    if kind == "album":
        children = api.get_album_tracks(item_id)
    elif kind == "playlist":
        children = api.get_playlist_items(item_id)
    elif kind == "artist":
        children = api.get_artist_albums(item_id)
    else:
        children = []
    return dict(item), [dict(c) for c in (children or [])]


# Fields that count as "meaningful" drift on a track snapshot. A
# rename or a re-tag is something the user wants to see reflected; a
# play count tick or a last-played timestamp isn't. We deliberately
# don't compare RunTimeTicks here even though duration is in the list
# below — see :data:`_BLOB_FIELDS` for why.
_META_FIELDS = (
    "Name",
    "AlbumArtist",
    "Album",
    "Artist",
    "Artists",
    "IndexNumber",
    "ParentIndexNumber",
)

# Fields whose change suggests the **server-side audio file** was
# re-encoded or replaced — so the local blob is now mismatched and the
# node should be flagged ``stale`` for the next download wave to
# refresh. Most providers don't expose a content hash; the best signal
# we have is the modification timestamp (Jellyfin: ``DateModified``,
# Subsonic: rarely populated) plus duration (a re-encode tends to drift
# RunTimeTicks by a few ticks). Comparing both lets us catch the cases
# where one is missing.
_BLOB_FIELDS = ("DateModified", "RunTimeTicks")


def _meaningful_meta_diff(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """True if any field in :data:`_META_FIELDS` differs between two
    snapshots in a way the user would care about (rename, re-tag,
    track-number shuffle). Missing-vs-present counts; ``None`` and ``""``
    are treated as equal so a server upgrading a previously-null field
    to an empty string doesn't ping every snapshot as stale."""
    def _norm(v):
        # Collapse only None/"" (so a null→"" server upgrade isn't "stale");
        # must NOT collapse a legitimate 0 (track/index number) to missing,
        # which `(v or None)` would.
        return None if v in (None, "") else v

    for key in _META_FIELDS:
        if _norm(old.get(key)) != _norm(new.get(key)):
            return True
    return False


def _blob_might_be_stale(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """Heuristic: the underlying server-side audio file probably
    changed. Most providers don't expose a content hash, so we fall
    back to ``DateModified`` (Jellyfin) + ``RunTimeTicks`` (any
    re-encode tends to drift the tick count). When both are missing on
    both sides we conservatively return False — better to under-report
    than to mark every blob stale on every repair."""
    for key in _BLOB_FIELDS:
        a = old.get(key)
        b = new.get(key)
        if a is None and b is None:
            continue
        if a != b:
            return True
    return False


def is_stale(item_id: str) -> bool:
    """Re-fetch ``item_id``'s metadata and compare against the stored
    snapshot; True if either user-visible metadata or the underlying
    blob fields drifted. Cheap to call — one provider round-trip plus a
    field compare. Returns False if no node exists (nothing to drift
    from) or the provider can't find the item (handled by
    :func:`resync` separately)."""
    from . import index

    stored = index.get_snapshot(item_id)
    if not stored:
        return False
    try:
        from jellytoast.providers import get_provider

        latest = get_provider().get_item(item_id) or {}
    except Exception:
        return False
    if not latest:
        return False
    return _meaningful_meta_diff(stored, latest) or _blob_might_be_stale(stored, latest)


def resync(item_id: str) -> Dict[str, Any]:
    """Re-fetch ``item_id`` from the provider and reconcile the stored
    snapshot. Returns a dict describing the outcome::

        {
            "updated":          True if the snapshot was rewritten,
            "marked_stale":     True if the local blob is now flagged
                                stale (blob fields drifted, or the
                                item no longer exists server-side),
            "deleted_server_side": True if the item is gone upstream,
            "error":            error message string on a fetch failure,
                                else None,
        }

    Manual, per-download — the v1 answer to snapshot drift. Idempotent:
    a re-sync on an already-fresh node is a no-op. The blob is **never**
    deleted here; surfacing the "no longer exists" case is the UI's job
    so the user can confirm before throwing bytes away.
    """
    from . import index

    out: Dict[str, Any] = {
        "updated": False,
        "marked_stale": False,
        "deleted_server_side": False,
        "error": None,
    }
    stored = index.get_snapshot(item_id)
    if not stored:
        # Nothing to resync — node never existed.
        return out

    try:
        from jellytoast.providers import get_provider

        latest = get_provider().get_item(item_id)
    except Exception as exc:
        out["error"] = str(exc)
        return out

    if not latest:
        # Server doesn't know this item anymore. Flag stale so the UI
        # can offer to delete, but leave the local blob alone — the
        # user's bytes, the user's call.
        node = index.get_node(item_id)
        if node and node.get("state") == index.DownloadState.COMPLETE:
            index.mark_stale(item_id)
            out["marked_stale"] = True
        out["deleted_server_side"] = True
        return out

    meta_drift = _meaningful_meta_diff(stored, latest)
    blob_drift = _blob_might_be_stale(stored, latest)

    if meta_drift:
        # Refresh the snapshot in place — same node row, new metadata.
        # State is left untouched (upsert_node preserves it) so a
        # ``complete`` node stays ``complete`` after a pure rename.
        node = index.get_node(item_id)
        kind = (node or {}).get("kind") or "track"
        index.upsert_node(item_id, kind, dict(latest), requested=False)
        out["updated"] = True

    if blob_drift:
        node = index.get_node(item_id)
        if node and node.get("state") == index.DownloadState.COMPLETE:
            index.mark_stale(item_id)
            out["marked_stale"] = True

    return out
