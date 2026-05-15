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
    filtered to one ``kind``. Scoped to the current server identity.

    Each row's ``metadata_json`` is decoded into a ``metadata`` dict and
    a convenience ``name`` is lifted out, so the downloads screen
    doesn't have to re-parse JSON per row."""
    ident = server_identity()
    sql = "SELECT * FROM nodes WHERE requested = 1 AND id LIKE ? "
    params: tuple = (f"{ident}:%",)
    if kind:
        sql += "AND kind = ? "
        params += (kind,)
    sql += "ORDER BY added_at DESC"

    out: List[Dict[str, Any]] = []
    for r in db.query(sql, params):
        row = dict(r)
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        row["metadata"] = meta
        row["name"] = meta.get("Name") or row.get("item_id", "")
        out.append(row)
    return out


def list_complete_items(kind: "Optional[str]" = None) -> List[Dict[str, Any]]:
    """Every node in state ``complete`` for the current server identity,
    newest first, optionally filtered to one ``kind``.

    Unlike :func:`list_requested` this returns *every* fully-downloaded
    node — both the user-requested ones and the cascade children (an
    album's tracks, an artist's albums) that were pulled in to satisfy
    a parent download. Used by the offline search path, which wants to
    surface anything the user can actually play locally, regardless of
    whether they explicitly asked for it.

    Each row's ``metadata_json`` is decoded into a ``metadata`` dict and
    a convenience ``name`` is lifted out, matching the shape returned
    by :func:`list_requested`."""
    ident = server_identity()
    sql = "SELECT * FROM nodes WHERE state = 'complete' AND id LIKE ? "
    params: tuple = (f"{ident}:%",)
    if kind:
        sql += "AND kind = ? "
        params += (kind,)
    sql += "ORDER BY added_at DESC"

    out: List[Dict[str, Any]] = []
    for r in db.query(sql, params):
        row = dict(r)
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        row["metadata"] = meta
        row["name"] = meta.get("Name") or row.get("item_id", "")
        out.append(row)
    return out


def get_node(item_id: str) -> "Optional[Dict[str, Any]]":
    """Raw node row for ``item_id`` under the current identity, or None."""
    rows = db.query("SELECT * FROM nodes WHERE id = ? LIMIT 1",
                    (node_id(item_id),))
    return dict(rows[0]) if rows else None


def _strip_identity(pk: str) -> str:
    """``node_id`` -> bare ``item_id``. Inverse of :func:`node_id` for
    the current server identity."""
    prefix = f"{server_identity()}:"
    return pk[len(prefix):] if pk.startswith(prefix) else pk


def children(item_id: str) -> List[str]:
    """``item_id`` values of the direct children of a node."""
    rows = db.query(
        "SELECT child_id FROM edges WHERE parent_id = ?",
        (node_id(item_id),),
    )
    return [_strip_identity(r["child_id"]) for r in rows]


def parents(item_id: str) -> List[str]:
    """``item_id`` values of the direct parents of a node — the edges
    pointing *in*. The manager walks these upward to propagate a
    finished track's completion to its album / artist / playlist."""
    rows = db.query(
        "SELECT parent_id FROM edges WHERE child_id = ?",
        (node_id(item_id),),
    )
    return [_strip_identity(r["parent_id"]) for r in rows]


_TERMINAL_STATES = ("complete", "failed", "stale")


def recompute_state(item_id: str) -> "Optional[str]":
    """Recompute a parent node's ``state`` from its children and write
    it back. ``complete`` when every child is complete; ``failed`` when
    every child is terminal and at least one isn't complete; otherwise
    ``downloading``. Returns the new state, or ``None`` for a node with
    no children (a leaf, or a parent whose snapshot hasn't expanded yet
    — left untouched). This is how artist -> album -> track completion
    rolls upward: a finished track recomputes its album, which if now
    complete recomputes the artist."""
    rows = db.query(
        "SELECT n.state AS state FROM edges e "
        "JOIN nodes n ON n.id = e.child_id WHERE e.parent_id = ?",
        (node_id(item_id),),
    )
    if not rows:
        return None
    states = [r["state"] for r in rows]
    if not all(s in _TERMINAL_STATES for s in states):
        new = "downloading"
    elif all(s == "complete" for s in states):
        new = "complete"
    else:
        new = "failed"
    set_state(item_id, new)
    return new


def refcount(node_pk: str) -> int:
    """Number of incoming edges for a node primary key — its parent
    count. ``refcount == 0`` after a delete means the node is an orphan
    and should be reaped."""
    rows = db.query("SELECT COUNT(*) AS n FROM edges WHERE child_id = ?",
                    (node_pk,))
    return int(rows[0]["n"]) if rows else 0


# ── Graph mutations ─────────────────────────────────────────────────────────

def upsert_node(item_id: str, kind: str, metadata: Dict[str, Any],
                requested: bool, state: str = "pending",
                conn: "Optional[sqlite3.Connection]" = None) -> str:
    """Insert or update a node; return its primary key.

    On insert the node gets ``state`` and ``added_at = now``. On update
    an existing node keeps its ``added_at`` **and its ``state``** — state
    is driven explicitly via :func:`set_state` so re-touching a node
    (e.g. an album re-download that re-walks an already-complete track)
    can't silently demote it back to ``pending``. ``requested`` only
    escalates 0 -> 1: a track pulled in as a child that the user later
    downloads directly becomes requested; the reverse never happens
    here. ``metadata`` is refreshed on every call — the snapshot is
    cheap to re-freeze and the freshest wins.

    Pass ``conn`` from inside an existing ``db.transaction()`` to batch
    writes — a planning walk for an artist hits this hundreds of times
    and one commit is ~100x faster than one-per-call."""
    pk = node_id(item_id)
    meta_json = json.dumps(metadata) if metadata is not None else None
    now = db.now_iso()
    if conn is not None:
        _upsert_node_inner(conn, pk, item_id, kind, meta_json, state,
                           requested, now)
        return pk
    with db.transaction() as c:
        _upsert_node_inner(c, pk, item_id, kind, meta_json, state,
                           requested, now)
    return pk


def _upsert_node_inner(conn, pk, item_id, kind, meta_json, state,
                       requested, now):
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


def link(parent_item_id: str, child_item_id: str,
         conn: "Optional[sqlite3.Connection]" = None) -> None:
    """Add a ``parent -> child`` edge under the current server identity.
    Idempotent — a track shared by two playlists just gains a second
    incoming edge, which is exactly the refcount the cascade delete
    reads. ``conn`` batches into an existing transaction (see
    :func:`upsert_node`)."""
    sql = "INSERT OR IGNORE INTO edges(parent_id, child_id) VALUES(?, ?)"
    args = (node_id(parent_item_id), node_id(child_item_id))
    if conn is not None:
        conn.execute(sql, args)
        return
    with db.transaction() as c:
        c.execute(sql, args)


def set_state(item_id: str, state: str) -> None:
    """Update ``nodes.state`` + ``updated_at`` for one node. The single
    authoritative way state moves through pending -> downloading ->
    complete / failed / stale."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE nodes SET state = ?, updated_at = ? WHERE id = ?",
            (state, db.now_iso(), node_id(item_id)),
        )


def mark_requested(item_id: str) -> None:
    """Escalate an existing node to ``requested = 1`` without touching
    its state or metadata. For the case where the user explicitly
    downloads something that's already on disk as a cascade child — it
    should now show in the downloads screen as its own entry. No-op if
    the node doesn't exist."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE nodes SET requested = 1, updated_at = ? WHERE id = ?",
            (db.now_iso(), node_id(item_id)),
        )


def cascade_delete(item_id: str) -> List[str]:
    """Delete a node and every child orphaned by its removal, in one
    transaction. Returns the ``rel_path`` of every blob whose row was
    removed, so the caller can unlink the files off the GUI thread.

    The walk: delete a node (SQLite ``ON DELETE CASCADE`` takes its
    edges and blob row with it), then for each former child check its
    remaining incoming-edge count — a child down to zero parents is an
    orphan and gets visited too. A track still reachable from another
    playlist keeps an edge, so it survives the deletion of one of its
    parents. This is the whole reason for the generic node graph
    (design doc §5.7)."""
    removed_paths: List[str] = []
    with db.transaction() as conn:
        to_visit = [node_id(item_id)]
        while to_visit:
            pk = to_visit.pop()
            kids = [r["child_id"] for r in conn.execute(
                "SELECT child_id FROM edges WHERE parent_id = ?", (pk,))]
            brow = conn.execute(
                "SELECT rel_path FROM blobs WHERE node_id = ?", (pk,)
            ).fetchone()
            if brow is not None:
                removed_paths.append(brow["rel_path"])
            conn.execute("DELETE FROM nodes WHERE id = ?", (pk,))
            for kid in kids:
                rc = conn.execute(
                    "SELECT COUNT(*) AS n FROM edges WHERE child_id = ?",
                    (kid,),
                ).fetchone()["n"]
                if rc == 0:
                    to_visit.append(kid)
    return removed_paths


def repair() -> Dict[str, int]:
    """Reconcile index against disk: drop ``blobs`` rows whose file is
    missing, re-link orphans, recompute ``bytes``. Returns a summary
    (rows dropped, sizes fixed, …). Phase 6."""
    raise NotImplementedError("offline.index.repair — Phase 6")
