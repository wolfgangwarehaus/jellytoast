"""SQLite store for downloads — one relational file, real foreign keys.

This is the direct fix for Finamp's stated mistake of spreading download
state across five key-value boxes that must be hand-kept in sync (design
doc §4, §5.1). One ``downloads.db``, three tables, FKs that make cascade
and orphan-cleanup a graph walk instead of per-type special-casing.

    nodes   one row per downloadable thing the user has touched.
            id is ``{server_identity}:{item_id}`` so a re-login or a
            Jellyfin->Subsonic switch can't collide ids. ``kind`` is
            data, not schema — "downloaded artists/genres" needs no
            migration. ``metadata_json`` is the authoritative offline
            snapshot (§5.2), independent of disk_cache's browse cache.

    edges   parent_id -> child_id (album->track, playlist->track,
            artist->album). Refcount = incoming edges; deletion is
            "drop node, drop now-orphaned children".

    blobs   node_id (a track) -> relative path + quality/codec/bytes.
            Audio files only. Paths are relative to locations.downloads_dir()
            and resolved at runtime — never absolute (the Finamp iOS lesson).

Schema version is tracked in ``PRAGMA user_version``; :data:`_MIGRATIONS`
is the ordered list applied on :func:`connect`. ``foreign_keys`` is ON
per-connection (SQLite default-off), so ``ON DELETE CASCADE`` actually
fires for the edge/blob cleanup.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .locations import db_path


def now_iso() -> str:
    """ISO-8601 UTC timestamp — the format every ``*_at`` column stores.
    One definition so ``index`` and ``store`` agree on it."""
    return datetime.now(timezone.utc).isoformat()


# ── Schema migrations ───────────────────────────────────────────────────────
#
# Each entry is applied in order when the DB's user_version is below its
# index+1. Never edit a shipped migration — append a new one. Phase 1
# ships v1 (the full base schema); later phases append columns/indexes.

def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE nodes (
            id            TEXT PRIMARY KEY,         -- {server_identity}:{item_id}
            item_id       TEXT NOT NULL,            -- provider item id, unprefixed
            kind          TEXT NOT NULL,            -- track|album|artist|playlist
            metadata_json TEXT,                     -- frozen snapshot dict (§5.2)
            state         TEXT NOT NULL DEFAULT 'pending',
                                                    -- pending|downloading|complete|failed|stale
            requested     INTEGER NOT NULL DEFAULT 0,
                                                    -- 1 = user asked, 0 = pulled in as child
            added_at      TEXT NOT NULL,            -- ISO-8601 UTC
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE edges (
            parent_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            child_id  TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            PRIMARY KEY (parent_id, child_id)
        );
        CREATE INDEX idx_edges_child ON edges(child_id);

        CREATE TABLE blobs (
            node_id       TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            rel_path      TEXT NOT NULL,            -- relative to downloads_dir()
            quality       TEXT,
            codec         TEXT,
            bytes         INTEGER,
            sha           TEXT,
            downloaded_at TEXT NOT NULL
        );

        CREATE INDEX idx_nodes_kind      ON nodes(kind);
        CREATE INDEX idx_nodes_state     ON nodes(state);
        CREATE INDEX idx_nodes_requested ON nodes(requested);
        CREATE INDEX idx_nodes_item_id   ON nodes(item_id);
        """
    )


# Ordered. _MIGRATIONS[i] takes the DB from user_version i to i+1.
_MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1,
]


# ── Connection management ───────────────────────────────────────────────────
#
# One connection, created lazily, reused for the process. SQLite handles
# concurrency via its own locking; we additionally guard with a lock
# because the download manager will touch the DB from the async_io pool
# while the GUI thread reads it. `check_same_thread=False` + the lock is
# the standard pattern for a shared sqlite3 connection.

_conn: "Optional[sqlite3.Connection]" = None
_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    """Return the process-wide connection, opening + migrating on first
    call. Idempotent. Enables ``foreign_keys`` and WAL (so a long blob
    write on the pool thread doesn't block GUI-thread reads)."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        conn = sqlite3.connect(str(db_path()), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _run_migrations(conn)
        _conn = conn
        return _conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply every migration above the current ``user_version``. Each
    runs in its own transaction so a failure leaves the version at the
    last good step rather than half-applied."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(_MIGRATIONS)):
        with conn:  # transaction
            _MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version = {i + 1}")


def transaction():
    """Context manager for a guarded write transaction:

        with db.transaction() as conn:
            conn.execute(...)

    Holds the module lock for the duration so pool-thread writes and
    GUI-thread reads don't interleave mid-statement, and commits /
    rolls back via sqlite3's connection-as-context-manager."""
    return _Transaction()


class _Transaction:
    def __enter__(self) -> sqlite3.Connection:
        _lock.acquire()
        self._conn = connect()
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._conn.__exit__(exc_type, exc, tb)
        finally:
            _lock.release()
        return False


def query(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    """Run a read query under the lock and return all rows. Convenience
    for the read-heavy index/store call sites."""
    with _lock:
        return connect().execute(sql, params).fetchall()


def close() -> None:
    """Close the connection — used by tests and on sign-out teardown."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
