"""Tests for the offline disk-reconciliation walk
(``jellytoast/offline/index.repair``).

This is the GC / data-integrity check: walk every ``blobs`` row, drop
the rows whose file is missing, fix ``bytes`` drift, surface (but never
delete) orphan files on disk, and flip ``complete`` track nodes whose
blob row vanished to ``failed`` so the next sync re-tries them.

Distinct from ``jellytoast.offline.repair`` — the snapshot/resync flavour
in ``__init__.py``. Both exist; only this one walks the filesystem.

Uses a local ``repair_env`` fixture — fresh DB + blob dir under a tmp
path, with the downloads dir as a sibling of the DB file (matching
production) so the on-disk walk doesn't trip over SQLite's WAL/SHM
files. Never touches the real downloads.db.
"""

from __future__ import annotations

import pytest

from jellytoast.offline import db as _db
from jellytoast.offline import index as _index
from jellytoast.offline import locations as _loc
from jellytoast.offline import store as _store


@pytest.fixture
def repair_env(tmp_path, monkeypatch):
    """A fresh offline env where the downloads dir is a child of the
    DB dir — matches production layout (``downloads.db`` is a sibling
    of ``downloads/``) so the on-disk walk doesn't trip over the
    SQLite WAL/SHM files."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(_loc, "db_path", lambda: tmp_path / "downloads.db")
    monkeypatch.setattr(_loc, "_DOWNLOADS_DIR", downloads)

    if _db._conn is not None:
        try:
            _db._conn.close()
        except Exception:
            pass
        _db._conn = None
    _db.connect()
    yield downloads
    if _db._conn is not None:
        try:
            _db._conn.close()
        except Exception:
            pass
        _db._conn = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _track_with_blob(item_id, *, size=8):
    """Create a complete track node and commit a real blob file for it
    via the public store API."""
    _index.upsert_node(item_id, "track", {"Name": item_id}, True, state="complete")
    part = _store.part_path_for(item_id, "flac")
    part.write_bytes(b"x" * size)
    _store.commit_blob(item_id, part, "original", "flac", size)


def _insert_blob_row(node_pk, rel_path, *, bytes_=0):
    """Bypass the store and write a raw ``blobs`` row — used to seed
    the broken states (file missing, bytes wrong) that the on-disk
    write path can't naturally produce."""
    with _db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blobs(node_id, rel_path, quality, "
            "codec, bytes, sha, downloaded_at) VALUES(?,?,?,?,?,?,?)",
            (node_pk, rel_path, "original", "flac", int(bytes_), None, _db.now_iso()),
        )


def _seed_node(item_id, *, kind="track", state="complete"):
    return _index.upsert_node(item_id, kind, {"Name": item_id}, True, state=state)


def _write_orphan(rel_path, payload=b"orphan"):
    """Drop a file under the downloads dir with no corresponding blob
    row. Used to seed the orphan-files branch."""
    abs_path = _loc.resolve(rel_path)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(payload)
    return abs_path


# ── empty state ─────────────────────────────────────────────────────────────


class TestEmpty:
    def test_empty_returns_all_zeros(self, repair_env):
        out = _index.repair()
        assert out == {
            "blobs_dropped": 0,
            "bytes_fixed": 0,
            "orphan_files": 0,
            "orphan_paths": [],
            "nodes_recovered": 0,
        }


# ── drop-row case (blob file missing) ──────────────────────────────────────


class TestDropRow:
    def test_missing_file_drops_blob_row(self, repair_env):
        pk = _seed_node("t1")
        _insert_blob_row(pk, "ab/deadbeef.flac", bytes_=100)
        # File deliberately not written — the row points at nothing.

        out = _index.repair()

        assert out["blobs_dropped"] == 1
        # Row really is gone.
        rows = _db.query("SELECT 1 FROM blobs WHERE node_id = ?", (pk,))
        assert rows == []

    def test_drop_row_also_recovers_widow_node(self, repair_env):
        # Dropping the blob row leaves a 'complete' track node with no
        # blob — that's broken state, flip it to 'failed'.
        pk = _seed_node("t1", state="complete")
        _insert_blob_row(pk, "ab/deadbeef.flac", bytes_=100)

        out = _index.repair()

        assert out["blobs_dropped"] == 1
        assert out["nodes_recovered"] == 1
        assert _index.get_node("t1")["state"] == "failed"


# ── bytes-fix case ─────────────────────────────────────────────────────────


class TestBytesFix:
    def test_wrong_bytes_is_rewritten(self, repair_env):
        _track_with_blob("t1", size=100)
        rel = _store.resolve("t1").path
        # Forcibly mis-record the size in the DB.
        with _db.transaction() as conn:
            conn.execute(
                "UPDATE blobs SET bytes = ? WHERE node_id = ?",
                (999, _index.node_id("t1")),
            )

        out = _index.repair()

        assert out["bytes_fixed"] == 1
        assert out["blobs_dropped"] == 0
        # Row now reflects the real size.
        rows = _db.query("SELECT bytes FROM blobs WHERE node_id = ?", (_index.node_id("t1"),))
        assert int(rows[0]["bytes"]) == rel.stat().st_size == 100

    def test_correct_bytes_is_not_counted(self, repair_env):
        _track_with_blob("t1", size=8)
        out = _index.repair()
        assert out["bytes_fixed"] == 0


# ── orphan-file case ───────────────────────────────────────────────────────


class TestOrphanFiles:
    def test_orphan_file_is_surfaced_and_kept(self, repair_env):
        abs_path = _write_orphan("ab/loose.flac")

        out = _index.repair()

        assert out["orphan_files"] == 1
        assert [x.replace("\\", "/") for x in out["orphan_paths"]] == ["ab/loose.flac"]
        # CRITICAL: never delete an orphan — the user decides.
        assert abs_path.is_file()

    def test_part_fragments_are_not_orphans(self, repair_env):
        # In-flight .part files belong to an active download, not to
        # a missing blob row.
        _write_orphan("ab/in-flight.flac.part")

        out = _index.repair()

        assert out["orphan_files"] == 0
        assert out["orphan_paths"] == []

    def test_known_blob_is_not_an_orphan(self, repair_env):
        _track_with_blob("t1", size=8)
        out = _index.repair()
        assert out["orphan_files"] == 0

    def test_orphan_paths_capped_at_100(self, repair_env):
        for i in range(150):
            _write_orphan(f"sh/orphan-{i:04d}.flac")

        out = _index.repair()

        assert out["orphan_files"] == 150
        assert len(out["orphan_paths"]) == 100


# ── broken-state node case ─────────────────────────────────────────────────


class TestBrokenStateNode:
    def test_complete_track_without_blob_is_flipped_to_failed(self, repair_env):
        # No blob row at all for this 'complete' track — broken state.
        _seed_node("t1", state="complete")

        out = _index.repair()

        assert out["nodes_recovered"] == 1
        assert _index.get_node("t1")["state"] == "failed"

    def test_complete_album_without_blob_is_left_alone(self, repair_env):
        # Container kinds (album/artist/playlist) legitimately have no
        # blob — they're 'complete' when their children are. Don't
        # flip them.
        _seed_node("album1", kind="album", state="complete")

        out = _index.repair()

        assert out["nodes_recovered"] == 0
        assert _index.get_node("album1")["state"] == "complete"

    def test_pending_node_without_blob_is_left_alone(self, repair_env):
        # Only 'complete' track nodes get the broken-state recovery —
        # a 'pending' node naturally has no blob yet.
        _seed_node("t1", state="pending")

        out = _index.repair()

        assert out["nodes_recovered"] == 0
        assert _index.get_node("t1")["state"] == "pending"


# ── combined / idempotence ─────────────────────────────────────────────────


class TestCombined:
    def test_all_four_conditions_in_one_pass(self, repair_env):
        # 1) drop-row: blob row pointing at a missing file
        pk1 = _seed_node("t-missing", state="complete")
        _insert_blob_row(pk1, "aa/missing.flac", bytes_=42)

        # 2) bytes-fix: real file, wrong bytes in DB
        _track_with_blob("t-bytes", size=50)
        with _db.transaction() as conn:
            conn.execute(
                "UPDATE blobs SET bytes = ? WHERE node_id = ?",
                (12345, _index.node_id("t-bytes")),
            )

        # 3) orphan-file: file on disk with no blob row
        _write_orphan("bb/orphan.flac")

        # 4) broken-state node: 'complete' track with no blob row
        _seed_node("t-widow", state="complete")

        out = _index.repair()

        assert out["blobs_dropped"] == 1
        assert out["bytes_fixed"] == 1
        assert out["orphan_files"] == 1
        assert [x.replace("\\", "/") for x in out["orphan_paths"]] == ["bb/orphan.flac"]
        # Two widows: the explicit one + the row whose blob was just
        # dropped (t-missing's node was 'complete' with no blob).
        assert out["nodes_recovered"] == 2

    def test_idempotent_second_call_is_a_no_op(self, repair_env):
        # Seed every condition, run once.
        pk1 = _seed_node("t-missing", state="complete")
        _insert_blob_row(pk1, "aa/missing.flac", bytes_=42)
        _track_with_blob("t-bytes", size=50)
        with _db.transaction() as conn:
            conn.execute(
                "UPDATE blobs SET bytes = ? WHERE node_id = ?",
                (12345, _index.node_id("t-bytes")),
            )
        _seed_node("t-widow", state="complete")

        first = _index.repair()
        assert first["blobs_dropped"] >= 1
        assert first["bytes_fixed"] >= 1
        assert first["nodes_recovered"] >= 1

        # Second call: everything fixable was fixed. Orphan files
        # remain (we never delete them) but the row-side counts are
        # all zero — the widow's state is now 'failed', not 'complete'.
        second = _index.repair()
        assert second["blobs_dropped"] == 0
        assert second["bytes_fixed"] == 0
        assert second["nodes_recovered"] == 0
