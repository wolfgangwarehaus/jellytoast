"""Blob storage — the audio files behind downloaded tracks.

``store`` owns the bytes on disk and the ``blobs`` table's view of them.
Its job is to keep the index and the filesystem honest: atomic writes
(``.part`` temp file -> rename, so an interrupted download leaves a
discardable fragment, never a half-file masquerading as complete),
relative-path persistence (resolved against ``locations.downloads_dir()``
at runtime), cover pinning, and disk-usage accounting.

Phase 1: :func:`resolve` and :func:`usage` are functional against the
real schema (empty until Phase 2). Write paths are skeletons.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import db, index
from .locations import resolve as _resolve_rel


@dataclass(frozen=True)
class Blob:
    """A resolved local audio file. ``path`` is absolute (the DB stores
    only ``rel_path``); ``as_uri()`` is what ``_build_now_playing`` hands
    to mpv in place of a stream URL."""
    node_id: str
    path: Path
    quality: str
    codec: str
    bytes: int

    def exists(self) -> bool:
        return self.path.is_file()

    def as_uri(self) -> str:
        return self.path.as_uri()


# ── Read paths (functional in Phase 1) ──────────────────────────────────────

def resolve(item_id: str) -> "Optional[Blob]":
    """Resolved :class:`Blob` for ``item_id`` under the current server
    identity, or ``None`` if there's no ``blobs`` row. Does **not**
    stat the file — callers that need a liveness guarantee check
    ``Blob.exists()`` (or rely on the repair walk to prune dead rows)."""
    rows = db.query(
        "SELECT * FROM blobs WHERE node_id = ? LIMIT 1",
        (index.node_id(item_id),),
    )
    if not rows:
        return None
    r = rows[0]
    return Blob(
        node_id=r["node_id"],
        path=_resolve_rel(r["rel_path"]),
        quality=r["quality"] or "",
        codec=r["codec"] or "",
        bytes=int(r["bytes"] or 0),
    )


def usage() -> Dict[str, int]:
    """Bytes on disk grouped by node kind, plus ``total``. Reads
    ``blobs.bytes`` joined to ``nodes.kind`` — the recorded sizes, not
    a filesystem walk (the repair walk is what reconciles drift)."""
    rows = db.query(
        """
        SELECT n.kind AS kind, COALESCE(SUM(b.bytes), 0) AS total
        FROM blobs b JOIN nodes n ON n.id = b.node_id
        GROUP BY n.kind
        """
    )
    out: Dict[str, int] = {r["kind"]: int(r["total"]) for r in rows}
    out["total"] = sum(out.values())
    return out


# ── Write paths (Phase 2/3 skeletons) ───────────────────────────────────────

def part_path_for(item_id: str, ext: str) -> Path:
    """Absolute ``.part`` temp path a download writes into before the
    atomic rename. Layout is ``<sha-shard>/<id>.<ext>.part`` so a big
    library doesn't pile thousands of files in one directory. Phase 2."""
    raise NotImplementedError("offline.store.part_path_for — Phase 2")


def commit_blob(item_id: str, part_path: Path, quality: str, codec: str,
                bytes_: int, sha: "Optional[str]" = None) -> str:
    """Atomically rename a completed ``.part`` into place and write the
    ``blobs`` row. Returns the persisted ``rel_path``. Phase 2."""
    raise NotImplementedError("offline.store.commit_blob — Phase 2")


def delete_blob(node_pk: str) -> None:
    """Unlink a blob file and drop its ``blobs`` row. Called off the
    GUI thread by the cascade-delete path. Phase 3."""
    raise NotImplementedError("offline.store.delete_blob — Phase 3")


def pin_cover(image_id: str) -> None:
    """Mark an ``image_id``'s cover as download-pinned so
    ``image_cache.py`` eviction can't reclaim it while a download that
    needs it exists. Covers already persist on disk (design doc §2/§3);
    downloads just protect the relevant ones. Phase 3."""
    raise NotImplementedError("offline.store.pin_cover — Phase 3")
