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


# ── Graph mutations (Phase 2/3 skeletons) ───────────────────────────────────

def upsert_node(item_id: str, kind: str, metadata: Dict[str, Any],
                requested: bool, state: str = "pending") -> str:
    """Insert or update a node; return its primary key. An existing
    node keeps its ``added_at`` and only escalates ``requested`` (a
    child later explicitly requested becomes requested; the reverse
    doesn't happen here). Phase 2."""
    raise NotImplementedError("offline.index.upsert_node — Phase 2")


def link(parent_item_id: str, child_item_id: str) -> None:
    """Add a ``parent -> child`` edge (idempotent). Phase 2/3."""
    raise NotImplementedError("offline.index.link — Phase 2")


def set_state(item_id: str, state: str) -> None:
    """Update ``nodes.state`` + ``updated_at`` for one node. Phase 2."""
    raise NotImplementedError("offline.index.set_state — Phase 2")


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
