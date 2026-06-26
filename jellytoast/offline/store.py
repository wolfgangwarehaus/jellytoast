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

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import db, index
from .locations import downloads_dir, to_relative
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


def subtree_bytes(item_id: str) -> int:
    """Total bytes of every blob at or below ``item_id`` in the edge
    graph — the on-disk size of a downloaded album / playlist / artist
    (or just the track itself for a leaf). Recursive CTE walk so an
    artist sums across all its albums' tracks in one query."""
    rows = db.query(
        """
        WITH RECURSIVE sub(id) AS (
            SELECT ?
            UNION
            SELECT e.child_id FROM edges e JOIN sub ON e.parent_id = sub.id
        )
        SELECT COALESCE(SUM(b.bytes), 0) AS total
        FROM blobs b JOIN sub ON b.node_id = sub.id
        """,
        (index.node_id(item_id),),
    )
    return int(rows[0]["total"]) if rows else 0


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


# ── Write paths ─────────────────────────────────────────────────────────────


def _blob_hash(item_id: str) -> str:
    """SHA1 of the node primary key — the on-disk filename stem. Stable
    per (server identity, item), filesystem-safe, and collision-free
    enough for a personal library."""
    return hashlib.sha1(index.node_id(item_id).encode("utf-8")).hexdigest()


def part_path_for(item_id: str, ext: str) -> Path:
    """Absolute ``.part`` temp path a download writes into before the
    atomic rename. Layout is ``<hh>/<sha>.<ext>.part`` — a two-char
    shard dir keeps a big library from piling thousands of files in one
    directory. The shard dir is created on call."""
    h = _blob_hash(item_id)
    ext = (ext or "audio").lstrip(".")
    shard = downloads_dir() / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    return shard / f"{h}.{ext}.part"


def commit_blob(
    item_id: str,
    part_path: Path,
    quality: str,
    codec: str,
    bytes_: int,
    sha: "Optional[str]" = None,
) -> str:
    """Atomically rename a completed ``.part`` into place and write (or
    replace) the ``blobs`` row. Returns the persisted relative path.

    ``os.replace`` is the atomic step — until it runs there is only a
    ``.part`` file and no ``blobs`` row, so an interrupted download is
    self-evidently incomplete and the next attempt just overwrites the
    fragment. The DB row is written only after the rename succeeds."""
    part_path = Path(part_path)
    final = part_path.with_suffix("")  # strip the trailing .part
    os.replace(part_path, final)  # atomic within a filesystem
    rel = to_relative(final)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blobs(node_id, rel_path, quality, "
            "codec, bytes, sha, downloaded_at) VALUES(?,?,?,?,?,?,?)",
            (index.node_id(item_id), rel, quality, codec, int(bytes_), sha, db.now_iso()),
        )
    return rel


def discard_part(part_path: Path) -> None:
    """Unlink a ``.part`` fragment — used when a download is cancelled
    mid-flight (``manager.remove`` while the GET was running) or fails
    in a way the next attempt won't resume. Best-effort."""
    try:
        Path(part_path).unlink(missing_ok=True)
    except OSError:
        pass


def adopt_orphan(item_id: str) -> bool:
    """Recover a download stranded by a crash between ``commit_blob``'s
    ``os.replace`` (the file is finalised) and its ``blobs`` INSERT: a
    complete blob file is on disk but no ``blobs`` row references it, and the
    node is stuck ``downloading``. If such an orphan exists, record the row
    and return True (re-downloading a finished file would be wasted work);
    return False otherwise. Called from ``manager.resume_pending`` at launch."""
    node = index.node_id(item_id)
    if db.query("SELECT 1 FROM blobs WHERE node_id = ? LIMIT 1", (node,)):
        return False  # already recorded — not an orphan
    h = _blob_hash(item_id)
    shard = downloads_dir() / h[:2]
    if not shard.is_dir():
        return False
    final = next(
        (
            p
            for p in shard.glob(f"{h}.*")
            if not p.name.endswith(".part") and p.is_file()
        ),
        None,
    )
    if final is None:
        return False
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blobs(node_id, rel_path, quality, "
            "codec, bytes, sha, downloaded_at) VALUES(?,?,?,?,?,?,?)",
            (
                node,
                to_relative(final),
                "original",
                final.suffix.lstrip("."),
                final.stat().st_size,
                None,
                db.now_iso(),
            ),
        )
    return True


def delete_files(rel_paths: "list[str]") -> int:
    """Unlink the given blob files (``blobs.rel_path`` values, as
    returned by ``index.cascade_delete``). The DB rows are already gone
    — ``cascade_delete`` dropped them via FK cascade — so this is pure
    filesystem cleanup, and it's called off the GUI thread because
    unlinking a big playlist's worth of files shouldn't stutter the UI
    (Finamp's lesson, design doc §5.7). Best-effort per file; returns
    the count actually removed. Also reaps a now-empty shard dir so the
    downloads tree doesn't accumulate empty two-char folders."""
    removed = 0
    for rel in rel_paths:
        path = _resolve_rel(rel)
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            shard = path.parent
            if shard != downloads_dir() and not any(shard.iterdir()):
                shard.rmdir()
        except OSError:
            pass
    return removed


def pin_cover(image_id: str) -> None:
    """Mark an ``image_id``'s cover as download-pinned so
    ``image_cache.py`` eviction can't reclaim it while a download that
    needs it exists. Covers already persist on disk (design doc §2/§3);
    downloads just protect the relevant ones. Phase 3."""
    raise NotImplementedError("offline.store.pin_cover — Phase 3")
