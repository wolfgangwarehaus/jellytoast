"""Node-graph operations over ``downloads.db``.

The index owns node identity and the parent/child graph: upsert a node,
link edges, cascade-delete with orphan cleanup, refcount, and the repair
walk. Keeping every graph mutation here (rather than scattered SQL at
call sites) is what makes "a track in two playlists is one blob with two
edges" a property of the system rather than a thing each caller must
remember.

Phase 1: identity + read queries are functional (they run against the
real, empty schema and return sane empties). Graph mutations and the
repair walk are skeletons — Phase 2/3/6 in the rollout.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import db


# ── Node identity ───────────────────────────────────────────────────────────

def server_identity() -> str:
    """Stable identity string for the current server + provider, so
    ``nodes.id`` keys are isolated per server and survive a re-login
    cleanly. Mirrors ``disk_cache._server_scope`` — defensively guarded
    so this module imports without a live settings store."""
    try:
        from modules.settings import get_settings
        s = get_settings()
        return f"{s.provider_kind or ''}|{s.server_url or ''}"
    except Exception:
        return ""


def node_id(item_id: str) -> str:
    """The ``nodes.id`` primary key for a provider ``item_id`` under the
    current server identity."""
    return f"{server_identity()}:{item_id}"


# ── Read queries (functional in Phase 1) ────────────────────────────────────

def is_complete(item_id: str) -> bool:
    """True if ``item_id`` has a node in state ``complete`` for the
    current server identity. Indexed lookup — safe on hot paths."""
    rows = db.query(
        "SELECT 1 FROM nodes WHERE id = ? AND state = 'complete' LIMIT 1",
        (node_id(item_id),),
    )
    return bool(rows)


def list_requested(kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """User-requested nodes (``requested = 1``), newest first, optionally
    filtered to one ``kind``. Scoped to the current server identity."""
    ident = server_identity()
    sql = (
        "SELECT * FROM nodes WHERE requested = 1 AND id LIKE ? "
    )
    params: tuple = (f"{ident}:%",)
    if kind:
        sql += "AND kind = ? "
        params += (kind,)
    sql += "ORDER BY added_at DESC"
    return [dict(r) for r in db.query(sql, params)]


def get_node(item_id: str) -> "Optional[Dict[str, Any]]":
    """Raw node row for ``item_id`` under the current identity, or None."""
    rows = db.query("SELECT * FROM nodes WHERE id = ? LIMIT 1",
                    (node_id(item_id),))
    return dict(rows[0]) if rows else None


def children(item_id: str) -> List[str]:
    """``item_id`` values of the direct children of a node."""
    ident = server_identity()
    rows = db.query(
        "SELECT child_id FROM edges WHERE parent_id = ?",
        (node_id(item_id),),
    )
    prefix = f"{ident}:"
    return [r["child_id"][len(prefix):] if r["child_id"].startswith(prefix)
            else r["child_id"] for r in rows]


def refcount(node_pk: str) -> int:
    """Number of incoming edges for a node primary key — its parent
    count. ``refcount == 0`` after a delete means the node is an orphan
    and should be reaped."""
    rows = db.query("SELECT COUNT(*) AS n FROM edges WHERE child_id = ?",
                    (node_pk,))
    return int(rows[0]["n"]) if rows else 0


# ── Graph mutations ─────────────────────────────────────────────────────────

def upsert_node(item_id: str, kind: str, metadata: Dict[str, Any],
                requested: bool, state: str = "pending") -> str:
    """Insert or update a node; return its primary key.

    On insert the node gets ``state`` and ``added_at = now``. On update
    an existing node keeps its ``added_at`` **and its ``state``** — state
    is driven explicitly via :func:`set_state` so re-touching a node
    (e.g. an album re-download that re-walks an already-complete track)
    can't silently demote it back to ``pending``. ``requested`` only
    escalates 0 -> 1: a track pulled in as a child that the user later
    downloads directly becomes requested; the reverse never happens
    here. ``metadata`` is refreshed on every call — the snapshot is
    cheap to re-freeze and the freshest wins."""
    pk = node_id(item_id)
    meta_json = json.dumps(metadata) if metadata is not None else None
    now = db.now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT requested FROM nodes WHERE id = ?", (pk,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO nodes(id, item_id, kind, metadata_json, state, "
                "requested, added_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (pk, item_id, kind, meta_json, state,
                 1 if requested else 0, now, now),
            )
        else:
            new_requested = 1 if (requested or row["requested"]) else 0
            conn.execute(
                "UPDATE nodes SET kind = ?, metadata_json = ?, "
                "requested = ?, updated_at = ? WHERE id = ?",
                (kind, meta_json, new_requested, now, pk),
            )
    return pk


def link(parent_item_id: str, child_item_id: str) -> None:
    """Add a ``parent -> child`` edge under the current server identity.
    Idempotent — a track shared by two playlists just gains a second
    incoming edge, which is exactly the refcount the cascade delete
    reads."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO edges(parent_id, child_id) VALUES(?, ?)",
            (node_id(parent_item_id), node_id(child_item_id)),
        )


def set_state(item_id: str, state: str) -> None:
    """Update ``nodes.state`` + ``updated_at`` for one node. The single
    authoritative way state moves through pending -> downloading ->
    complete / failed / stale."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE nodes SET state = ?, updated_at = ? WHERE id = ?",
            (state, db.now_iso(), node_id(item_id)),
        )


def cascade_delete(item_id: str) -> List[str]:
    """Delete a node and every child orphaned by its removal (refcount
    -> 0). Returns the list of node primary keys actually removed so
    the caller can unlink their blob files off the GUI thread. A track
    still reachable from another parent survives. Phase 3."""
    raise NotImplementedError("offline.index.cascade_delete — Phase 3")


def repair() -> Dict[str, int]:
    """Reconcile index against disk: drop ``blobs`` rows whose file is
    missing, re-link orphans, recompute ``bytes``. Returns a summary
    (rows dropped, sizes fixed, …). Phase 6."""
    raise NotImplementedError("offline.index.repair — Phase 6")
