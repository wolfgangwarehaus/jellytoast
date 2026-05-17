"""Tests for the offline blob store (``modules/offline/store.py``).

The store owns the audio bytes on disk and the ``blobs`` table's view
of them: atomic ``.part`` -> rename commits, relative-path persistence,
the recursive size walk Phase 4's "how big is this album" read uses,
and the off-thread file cleanup behind a cascade delete.

Uses the shared ``offline_db`` fixture (conftest.py) — fresh DB + blob
dir under a tmp path, never the real downloads.db.
"""

from __future__ import annotations

from modules.offline import index as _index
from modules.offline import store as _store


# ── helpers ─────────────────────────────────────────────────────────────────


def _track_with_blob(item_id, *, size=8, kind="track"):
    """Create a complete node and commit a real blob file for it.
    Exercises the actual write path (part_path_for + commit_blob)."""
    _index.upsert_node(item_id, kind, {"Name": item_id}, True, state="complete")
    part = _store.part_path_for(item_id, "flac")
    part.write_bytes(b"x" * size)
    _store.commit_blob(item_id, part, "original", "flac", size)


def _album_with_tracks(album_id, specs):
    """specs: [(track_id, size), ...]."""
    _index.upsert_node(album_id, "album", {"Name": album_id}, True)
    for tid, size in specs:
        _track_with_blob(tid, size=size)
        _index.link(album_id, tid)


# ── _blob_hash / part_path_for ──────────────────────────────────────────────


class TestBlobPaths:
    def test_blob_hash_is_stable(self, offline_db):
        assert _store._blob_hash("t1") == _store._blob_hash("t1")
        assert _store._blob_hash("t1") != _store._blob_hash("t2")

    def test_part_path_layout(self, offline_db):
        part = _store.part_path_for("t1", "flac")
        assert part.suffix == ".part"
        assert part.name.endswith(".flac.part")
        # Two-char shard dir, created on call.
        assert part.parent.name == _store._blob_hash("t1")[:2]
        assert part.parent.is_dir()

    def test_part_path_normalises_extension(self, offline_db):
        # leading dot stripped; empty ext falls back to "audio"
        assert _store.part_path_for("t1", ".mp3").name.endswith(".mp3.part")
        assert _store.part_path_for("t1", "").name.endswith(".audio.part")


# ── commit_blob ─────────────────────────────────────────────────────────────


class TestCommitBlob:
    def test_commit_renames_part_and_writes_row(self, offline_db):
        _index.upsert_node("t1", "track", {"Name": "t1"}, True, state="complete")
        part = _store.part_path_for("t1", "flac")
        part.write_bytes(b"audio-bytes")
        rel = _store.commit_blob("t1", part, "original", "flac", 11)
        # .part is gone, the final file is in place.
        assert not part.exists()
        final = part.with_suffix("")
        assert final.is_file()
        assert final.read_bytes() == b"audio-bytes"
        # Returned rel path round-trips through resolve.
        assert rel == "/".join((final.parent.name, final.name))

    def test_commit_blob_is_resolvable(self, offline_db):
        _track_with_blob("t1", size=12)
        blob = _store.resolve("t1")
        assert blob is not None
        assert blob.bytes == 12
        assert blob.codec == "flac"
        assert blob.quality == "original"
        assert blob.exists() is True
        assert blob.as_uri().startswith("file://")


# ── resolve ─────────────────────────────────────────────────────────────────


class TestResolve:
    def test_resolve_missing_returns_none(self, offline_db):
        assert _store.resolve("nope") is None

    def test_resolve_node_without_blob_returns_none(self, offline_db):
        # A node can exist (e.g. pending) with no blob row yet.
        _index.upsert_node("t1", "track", {}, True, state="pending")
        assert _store.resolve("t1") is None


# ── subtree_bytes ───────────────────────────────────────────────────────────


class TestSubtreeBytes:
    def test_unknown_item_is_zero(self, offline_db):
        assert _store.subtree_bytes("ghost") == 0

    def test_leaf_track_is_its_own_size(self, offline_db):
        _track_with_blob("t1", size=42)
        assert _store.subtree_bytes("t1") == 42

    def test_album_sums_its_tracks(self, offline_db):
        _album_with_tracks("album1", [("t1", 10), ("t2", 25), ("t3", 5)])
        assert _store.subtree_bytes("album1") == 40

    def test_artist_sums_recursively_across_albums(self, offline_db):
        _index.upsert_node("artist1", "artist", {"Name": "A"}, True)
        _album_with_tracks("album1", [("t1", 100), ("t2", 100)])
        _album_with_tracks("album2", [("t3", 50)])
        _index.link("artist1", "album1")
        _index.link("artist1", "album2")
        assert _store.subtree_bytes("artist1") == 250

    def test_shared_track_not_double_counted(self, offline_db):
        # The recursive CTE uses UNION, so a track reachable from two
        # playlists is summed once, not twice.
        _track_with_blob("t1", size=30)
        for pid in ("p1", "p2"):
            _index.upsert_node(pid, "playlist", {"Name": pid}, True)
            _index.link(pid, "t1")
        _index.upsert_node("root", "playlist", {"Name": "root"}, True)
        _index.link("root", "p1")
        _index.link("root", "p2")
        assert _store.subtree_bytes("root") == 30


# ── usage ───────────────────────────────────────────────────────────────────


class TestUsage:
    def test_empty_usage(self, offline_db):
        assert _store.usage() == {"total": 0}

    def test_usage_grouped_by_kind(self, offline_db):
        _album_with_tracks("album1", [("t1", 10), ("t2", 20)])
        u = _store.usage()
        # Tracks carry the blobs; the album node has no blob of its own.
        assert u["track"] == 30
        assert u["total"] == 30


# ── discard_part / delete_files ─────────────────────────────────────────────


class TestFileCleanup:
    def test_discard_part_removes_fragment(self, offline_db):
        part = _store.part_path_for("t1", "flac")
        part.write_bytes(b"fragment")
        _store.discard_part(part)
        assert not part.exists()

    def test_discard_part_missing_is_noop(self, offline_db):
        # Best-effort — must not raise on an already-gone fragment.
        _store.discard_part(_store.part_path_for("t1", "flac"))

    def test_delete_files_unlinks_and_counts(self, offline_db):
        _track_with_blob("t1", size=8)
        _track_with_blob("t2", size=8)
        rels = [_store.resolve("t1").path, _store.resolve("t2").path]
        from modules.offline.locations import to_relative

        rel_paths = [to_relative(p) for p in rels]
        removed = _store.delete_files(rel_paths)
        assert removed == 2
        assert not rels[0].exists() and not rels[1].exists()

    def test_delete_files_missing_is_tolerated(self, offline_db):
        # A rel_path with no file on disk just isn't counted.
        assert _store.delete_files(["ab/deadbeef.flac"]) == 0

    def test_delete_files_reaps_empty_shard_dir(self, offline_db):
        _track_with_blob("t1", size=8)
        blob_path = _store.resolve("t1").path
        shard = blob_path.parent
        from modules.offline.locations import to_relative

        _store.delete_files([to_relative(blob_path)])
        # The two-char shard dir is gone once its last file leaves.
        assert not shard.exists()
