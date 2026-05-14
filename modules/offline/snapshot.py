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


def freeze(item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Resolve ``item`` into its authoritative snapshot: the parent's
    own metadata dict plus the list of child item dicts (tracks for an
    album/playlist, albums for an artist; a track returns itself and an
    empty child list). The provider round-trips happen here, off the
    GUI thread via ``async_io``. Returns ``(parent_meta, children)``
    ready for ``index.upsert_node`` + ``index.link``. Phase 2."""
    raise NotImplementedError("offline.snapshot.freeze — Phase 2")


def is_stale(item_id: str) -> bool:
    """Re-fetch ``item_id``'s metadata and compare against the stored
    snapshot; True if the source changed (renamed track, edited
    playlist). Drives the ``nodes.state = 'stale'`` flag surfaced in
    the downloads screen. Phase 6."""
    raise NotImplementedError("offline.snapshot.is_stale — Phase 6")


def resync(item_id: str) -> None:
    """Re-freeze a download's metadata from the server, clearing a
    ``stale`` flag. Manual, per-download — the v1 answer to snapshot
    drift. Phase 6."""
    raise NotImplementedError("offline.snapshot.resync — Phase 6")
