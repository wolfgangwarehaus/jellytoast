"""Tests for the offline DB layer (``jellytoast/offline/db.py``).

``db`` owns the single process-wide SQLite connection, the migration
runner, and the guarded transaction / query helpers. The shared
``offline_db`` fixture (conftest.py) has already opened a fresh DB
against a tmp path by the time these run.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from jellytoast.offline import db as _db


class TestNowIso:
    def test_now_iso_is_parseable_utc(self):
        ts = _db.now_iso()
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None  # carries an offset


class TestConnect:
    def test_connect_is_idempotent(self, offline_db):
        assert _db.connect() is _db.connect()

    def test_schema_tables_exist(self, offline_db):
        names = {
            r["name"] for r in _db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"nodes", "edges", "blobs"} <= names

    def test_user_version_is_at_latest_migration(self, offline_db):
        version = _db.connect().execute("PRAGMA user_version").fetchone()[0]
        assert version == len(_db._MIGRATIONS)

    def test_foreign_keys_enabled(self, offline_db):
        on = _db.connect().execute("PRAGMA foreign_keys").fetchone()[0]
        assert on == 1


class TestTransaction:
    def test_transaction_commits_on_success(self, offline_db):
        with _db.transaction() as conn:
            conn.execute(
                "INSERT INTO nodes(id, item_id, kind, state, requested, "
                "added_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                ("k:t1", "t1", "track", "pending", 0, "t", "t"),
            )
        rows = _db.query("SELECT id FROM nodes WHERE id = ?", ("k:t1",))
        assert len(rows) == 1

    def test_transaction_rolls_back_on_exception(self, offline_db):
        with pytest.raises(RuntimeError):
            with _db.transaction() as conn:
                conn.execute(
                    "INSERT INTO nodes(id, item_id, kind, state, "
                    "requested, added_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("k:t2", "t2", "track", "pending", 0, "t", "t"),
                )
                raise RuntimeError("boom")
        # The insert above must have been rolled back.
        assert _db.query("SELECT 1 FROM nodes WHERE id = ?", ("k:t2",)) == []

    def test_fk_cascade_drops_edges_with_node(self, offline_db):
        # ON DELETE CASCADE on edges — deleting a node takes its edges.
        with _db.transaction() as conn:
            for nid in ("k:a", "k:b"):
                conn.execute(
                    "INSERT INTO nodes(id, item_id, kind, state, "
                    "requested, added_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (nid, nid, "track", "pending", 0, "t", "t"),
                )
            conn.execute("INSERT INTO edges(parent_id, child_id) VALUES(?,?)", ("k:a", "k:b"))
        with _db.transaction() as conn:
            conn.execute("DELETE FROM nodes WHERE id = ?", ("k:a",))
        assert _db.query("SELECT 1 FROM edges") == []


class TestQuery:
    def test_query_returns_row_objects(self, offline_db):
        rows = _db.query("SELECT 1 AS one, 2 AS two")
        assert rows[0]["one"] == 1
        assert rows[0]["two"] == 2

    def test_query_empty_result(self, offline_db):
        assert _db.query("SELECT 1 WHERE 1 = 0") == []


class TestTransactionLockSafety:
    def test_lock_released_when_enter_raises(self, offline_db, monkeypatch):
        """#2: if connect()/BEGIN raises inside __enter__, __exit__ is
        never called, so the transaction must release the RLock itself —
        else every later transaction/query/close deadlocks."""

        def _boom():
            raise RuntimeError("connect failed")

        monkeypatch.setattr(_db, "connect", _boom)
        with pytest.raises(RuntimeError):
            with _db.transaction():
                pass  # pragma: no cover — __enter__ raises first
        # Lock must be free: acquire non-blocking succeeds, then release.
        assert _db._lock.acquire(blocking=False) is True
        _db._lock.release()


class TestMigrationIdempotency:
    def test_rerun_v1_with_tables_present_does_not_crash(self, offline_db):
        """#53: executescript auto-commits, so a crash between the CREATEs
        and the user_version bump can leave the tables committed at
        version 0. Re-running the migration (IF NOT EXISTS) must be a safe
        no-op rather than 'table nodes already exists'."""
        from jellytoast.offline.db import _migrate_v1

        conn = _db.connect()
        # Simulate the crash window: tables exist, user_version reset to 0.
        conn.execute("PRAGMA user_version = 0")
        _migrate_v1(conn)  # must NOT raise
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchall()
        assert rows  # nodes table still present, no crash


class TestMigrationV3Backfill:
    """v3 splits the server identity out of ``nodes.id`` into dedicated
    ``provider_kind`` / ``server_url`` columns. A real user upgrades from a
    populated v2 DB, so the backfill must reconstruct those columns from
    each existing id — and it must strip the KNOWN ``item_id`` rather than
    the last ':', so an id whose URL *and* item_id both contain ':' still
    splits correctly (the exact conflation the refactor exists to kill)."""

    def _v2_conn(self):
        import sqlite3

        from jellytoast.offline.db import _migrate_v1, _migrate_v2

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _migrate_v1(conn)
        _migrate_v2(conn)  # now at the pre-v3 schema, no scope columns
        return conn

    def _insert_old(self, conn, pk, item_id, kind="track"):
        conn.execute(
            "INSERT INTO nodes(id, item_id, kind, state, requested, added_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pk, item_id, kind, "complete", 1, "t", "t"),
        )

    def test_backfill_splits_host_port_url(self):
        from jellytoast.offline.db import _migrate_v3

        conn = self._v2_conn()
        # url with a ':' (host:port), simple item_id.
        self._insert_old(conn, "jellyfin|http://h:8096:track-1", "track-1")
        _migrate_v3(conn)
        r = conn.execute(
            "SELECT provider_kind, server_url FROM nodes WHERE item_id = 'track-1'"
        ).fetchone()
        assert (r["provider_kind"], r["server_url"]) == ("jellyfin", "http://h:8096")

    def test_backfill_strips_known_item_id_not_last_colon(self):
        from jellytoast.offline.db import _migrate_v3

        conn = self._v2_conn()
        # BOTH the url and the item_id carry ':'. A naive rsplit(':', 1)
        # would mangle this; stripping the known item_id is exact.
        self._insert_old(conn, "subsonic|http://nas:al:bum:99", "al:bum:99", kind="album")
        _migrate_v3(conn)
        r = conn.execute(
            "SELECT provider_kind, server_url FROM nodes WHERE item_id = 'al:bum:99'"
        ).fetchone()
        assert (r["provider_kind"], r["server_url"]) == ("subsonic", "http://nas")

    def test_backfill_handles_empty_identity(self):
        from jellytoast.offline.db import _migrate_v3

        conn = self._v2_conn()
        # A node written while settings were unavailable: identity "" →
        # id is ":item". Must backfill to empty scope, not crash.
        self._insert_old(conn, ":orphan", "orphan")
        _migrate_v3(conn)
        r = conn.execute(
            "SELECT provider_kind, server_url FROM nodes WHERE item_id = 'orphan'"
        ).fetchone()
        assert (r["provider_kind"], r["server_url"]) == ("", "")
