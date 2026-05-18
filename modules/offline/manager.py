"""Download manager — the queue that turns "Download this" into bytes.

Sits on top of ``snapshot`` (freeze metadata), ``index`` (node graph),
and ``store`` (blob files). Its own job is orchestration: a queue with a
concurrency cap, progress, cascade expansion, and writing per-node state
back to ``nodes.state``.

**Getting the bytes** (design doc §5.3): an independent background HTTP
GET of ``get_audio_stream_url(item_id)``, streamed in chunks into a
``.part`` temp file, atomic-renamed on completion. Decoupled from mpv
entirely — a pause/seek/skip can't corrupt it. The blocking GET runs on
the shared ``async_io`` pool; the manager keeps its own queue and only
lets ``_MAX_CONCURRENT`` downloads occupy pool workers at once, so a big
album/playlist download can't starve quick ops (lyrics, favourite
toggle) of the 4-worker pool.

**Cascade** (design doc §5.1/§10): downloading an album/playlist/artist
expands it through ``snapshot.freeze`` into the node graph — artist ->
albums -> tracks — linking ``edges`` as it goes. Only the leaf tracks
get blob downloads; a finished track rolls its completion upward via
``index.recompute_state`` so the album/artist node resolves too.

**Threading**: all queue bookkeeping below (`_queue`, `_active`,
`_jobs`, `_pending`, `_cancelled`) is touched **only on the GUI thread**
— from ``enqueue``'s planning callback, ``_dispatch``, the per-download
result/error callbacks, and ``remove``. The worker threads only touch
the filesystem and emit the (thread-safe, queued) ``download_progress``
signal. So no lock is needed here, matching the image loader's
GUI-thread-only ``_inflight_subscribers`` pattern.

Phase 3: cascade + deletion. Quality note (unchanged from Phase 2):
downloads reuse ``get_audio_stream_url`` as-is, honouring the *streaming*
``audio_quality`` setting; a separate ``download_quality`` is Phase 6.
Pause/resume/retry and Wi-Fi-only gating are also Phase 6.
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

# Content-Type -> file extension. Original-format streams (Jellyfin
# static=true, Subsonic format=raw) report the real container here;
# the item's own ``Container`` field is the fallback when a server
# sends something generic like application/octet-stream.
_CTYPE_EXT = {
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}

# Emit a progress signal at most every ~2% so a fast download doesn't
# flood the bus with hundreds of updates per second.
_PROGRESS_STEP = 0.02

# How many downloads may occupy async_io pool workers at once. The pool
# has 4 workers; capping at 2 leaves headroom for quick ops so an album
# download never wedges a favourite-toggle behind it.
_MAX_CONCURRENT = 2

# ── Queue state (GUI-thread-only — see module docstring) ────────────────────
_queue: "Deque[str]" = collections.deque()  # track item_ids waiting
_active: "Set[str]" = set()  # track item_ids in flight
_jobs: "Dict[str, Dict[str, Any]]" = {}  # track_id -> {item, parents}
_cancelled: "Set[str]" = set()  # track_ids removed mid-flight
# top_parent_id -> {total, remaining} — drives the aggregate
# download_progress signal for a cascade's user-requested root node.
_pending: "Dict[str, Dict[str, int]]" = {}

# Queue-level pause flag. When True, ``_dispatch`` will not pop new jobs —
# any in-flight blob already on the pool runs to completion (a partial
# blob would be wasted bytes) and the queue idles. Re-hydrated from
# QSettings on first use so a paused queue survives a restart.
_paused: bool = False
_paused_loaded: bool = False

# Wi-Fi-only gate. ``_wifi_only`` is the user's persisted preference;
# ``_on_metered`` is the transient "we appear to be on a metered or
# cellular connection right now" stub, flipped by ``mark_metered`` (the
# integration seam for a future auto-detect layer). ``_dispatch`` blocks
# only when both are True — flipping the user toggle on while you're on
# Wi-Fi is a no-op until the network changes underneath you.
_wifi_only: bool = False
_wifi_only_loaded: bool = False
_on_metered: bool = False

# Smart-retry backoff schedule: ``2 ** min(retry_count, _BACKOFF_MAX_EXP)
# * _BACKOFF_BASE_S`` seconds. With base 30 and cap 6 the windows are
# 30, 60, 120, 240, 480, 960, 1920, then 1920 forever — re-fails stop
# pounding the network without ever locking the user out completely.
_BACKOFF_BASE_S = 30
_BACKOFF_MAX_EXP = 6


def _ext_for(content_type: str, container_hint: str) -> str:
    """Best-guess file extension from the response Content-Type, falling
    back to the item's ``Container`` metadata, then a neutral default."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CTYPE_EXT:
        return _CTYPE_EXT[ct]
    hint = (container_hint or "").strip().lower().lstrip(".")
    return hint or "audio"


def backoff_for(retry_count: int) -> int:
    """Backoff window in seconds for the ``retry_count``-th failure
    (1-indexed: ``retry_count = 1`` is the first failure)."""
    n = max(0, retry_count - 1)
    return _BACKOFF_BASE_S * (2 ** min(n, _BACKOFF_MAX_EXP))


def _record_failure(item_id: str) -> None:
    """Mark a node as failed and stamp the next backoff window. Atomic
    on the DB side — the index helper bumps ``retry_count`` and writes
    ``retry_after_ts`` in one transaction."""
    from . import index

    now = int(time.time())
    # Read the prior count up-front so we pick the *next* window based
    # on what this failure makes the count, not the prior value.
    row = index.get_node(item_id)
    prior = int(row.get("retry_count") or 0) if row else 0
    window = backoff_for(prior + 1)
    index.record_failure(item_id, now + window)
    index.set_state(item_id, "failed")


# ── Public entry points ─────────────────────────────────────────────────────


def enqueue(item: Dict[str, Any]) -> None:
    """Queue ``item`` (a track / album / playlist / artist dict) for
    download. Returns immediately; planning (the provider round-trips
    that expand a cascade) happens on a worker, then the leaf-track
    blob downloads run through the queue. Progress and terminal state
    are reported on the ``download_progress`` bus signal —
    ``(item_id, state, fraction)`` — for both the leaf tracks and the
    user-requested root node."""
    from . import snapshot, index

    item_id = item.get("Id", "")
    if not item_id:
        return
    kind = snapshot.kind_of(item)

    from modules.player_state import PlayerBus

    bus = PlayerBus.get()

    # Fast path: already fully downloaded (the node resolved to
    # ``complete``). A double-click on Download just re-confirms — but
    # still escalate ``requested`` so explicitly downloading something
    # that was only on disk as a cascade child lists it in its own
    # right.
    if index.is_complete(item_id):
        index.mark_requested(item_id)
        bus.download_progress.emit(item_id, "complete", 1.0)
        return

    bus.download_progress.emit(item_id, "pending", 0.0)

    def _do_plan() -> List[Dict[str, Any]]:
        return _plan(item, requested=True)

    def _planned(leaves: List[Dict[str, Any]]) -> None:
        _ingest_plan(item_id, kind, leaves)

    def _plan_err(exc: Exception) -> None:
        _record_failure(item_id)
        bus.download_progress.emit(item_id, "failed", 0.0)
        print(f"[jellytoast] download planning failed for {item_id}: {exc}", flush=True)

    from modules.async_io import run_async

    run_async(_do_plan, on_result=_planned, on_error=_plan_err)


def remove(item_id: str) -> None:
    """Delete a download and cascade. Cancels any queued or in-flight
    work under ``item_id``, drops the subtree from the index (orphaned
    children only — a track still in another playlist survives), and
    unlinks the blob files off the GUI thread. Emits
    ``download_progress(item_id, "removed", 0.0)``."""
    from . import index, store

    # Gather every descendant item_id (plus the root) so queued/active
    # jobs under it can be cancelled before the index rows vanish.
    subtree = _collect_subtree(item_id)
    for tid in subtree:
        if tid in _queue:
            # deque has no fast remove-by-value, but the queue is small
            # (only not-yet-started tracks) so a rebuild is cheap.
            try:
                _queue.remove(tid)
            except ValueError:
                pass
        if tid in _active:
            _cancelled.add(tid)  # let the in-flight GET unwind
        _jobs.pop(tid, None)
        _pending.pop(tid, None)

    rel_paths = index.cascade_delete(item_id)

    from modules.player_state import PlayerBus

    PlayerBus.get().download_progress.emit(item_id, "removed", 0.0)

    if rel_paths:
        from modules.async_io import run_async

        run_async(lambda: store.delete_files(rel_paths))


# ── Planning (worker thread) ────────────────────────────────────────────────


def _plan(item: Dict[str, Any], requested: bool) -> List[Dict[str, Any]]:
    """Recursively expand ``item`` into the node graph and return the
    flat list of leaf-track item dicts to download. Runs on a worker —
    ``snapshot.freeze`` does provider round-trips for cascade kinds.

    Network walk first (no DB lock held), then one transaction commits
    every node + edge — ~100x faster than a transaction per call for a
    full-artist cascade."""
    from . import db, snapshot, index

    nodes: List[tuple] = []  # (item_id, kind, metadata, requested)
    edges: List[tuple] = []  # (parent_id, child_id)
    leaves: List[Dict[str, Any]] = []

    def _walk(it: Dict[str, Any], req: bool) -> None:
        item_id = it.get("Id", "")
        kind = snapshot.kind_of(it)
        parent_meta, children = snapshot.freeze(it)
        nodes.append((item_id, kind, parent_meta, req))
        if kind == "track":
            leaves.append(it)
            return
        if not children:
            return
        for child in children:
            child_id = child.get("Id", "")
            if not child_id:
                continue
            _walk(child, False)
            edges.append((item_id, child_id))

    _walk(item, requested)

    # Single transaction for the whole plan.
    with db.transaction() as conn:
        for item_id, kind, meta, req in nodes:
            index.upsert_node(item_id, kind, meta, requested=req, conn=conn)
        for parent_id, child_id in edges:
            index.link(parent_id, child_id, conn=conn)
    return leaves


# ── Ingest + dispatch (GUI thread) ──────────────────────────────────────────


def _ingest_plan(top_id: str, top_kind: str, leaves: List[Dict[str, Any]]) -> None:
    """Turn a finished plan into queued jobs. Dedupes leaf tracks (a
    track listed twice in a playlist, or shared across an artist's
    albums, is one job), registers the aggregate-progress counter for a
    cascade root, and kicks the dispatcher."""
    from . import index
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()

    # Dedupe by item id, preserving order.
    unique: "Dict[str, Dict[str, Any]]" = {}
    for leaf in leaves:
        lid = leaf.get("Id", "")
        if lid and lid not in unique:
            unique[lid] = leaf

    is_cascade = top_kind != "track"
    pending_ids = [lid for lid in unique if not index.is_complete(lid)]

    if is_cascade:
        total = len(unique)
        remaining = len(pending_ids)
        if total == 0 or remaining == 0:
            # Nothing to fetch — every track was already on disk.
            state = index.recompute_state(top_id) or "complete"
            bus.download_progress.emit(top_id, state, 1.0)
            return
        _pending[top_id] = {"total": total, "remaining": remaining}
        bus.download_progress.emit(top_id, "downloading", 0.0)

    for lid in pending_ids:
        leaf = unique[lid]
        if lid in _jobs:
            # Already queued/active from another request — just attach
            # this root so it gets notified when the track finishes.
            if is_cascade:
                _jobs[lid]["parents"].add(top_id)
            continue
        _jobs[lid] = {
            "item": leaf,
            "parents": {top_id} if is_cascade else set(),
        }
        _queue.append(lid)

    _dispatch()


def _dispatch() -> None:
    """Start downloads up to the concurrency cap. Called whenever a slot
    might have opened — after ingest and after every terminal. Honours
    the queue-level ``_paused`` flag: a paused queue lets in-flight jobs
    finish but never pops the next. Also honours the Wi-Fi-only gate
    when the network is flagged metered."""
    _load_paused_once()
    _load_wifi_only_once()
    if _paused:
        return
    if _wifi_only and _on_metered:
        return
    while len(_active) < _MAX_CONCURRENT and _queue:
        tid = _queue.popleft()
        if tid in _cancelled or tid not in _jobs:
            _cancelled.discard(tid)
            continue
        _active.add(tid)
        _start_download(tid)


def _start_download(tid: str) -> None:
    """Resolve the stream URL and fire the background GET for one track."""
    from . import index
    from modules.player_state import PlayerBus
    from modules.providers import get_provider
    from modules.settings import get_settings

    bus = PlayerBus.get()

    # Subsonic rotates salt/token per request — resolve at fetch time.
    url = get_provider().get_audio_stream_url(tid, quality=get_settings().download_quality)
    if not url:
        _record_failure(tid)
        bus.download_progress.emit(tid, "failed", 0.0)
        _finish(tid, success=False)
        return

    index.set_state(tid, "downloading")
    bus.download_progress.emit(tid, "downloading", 0.0)
    container = _jobs.get(tid, {}).get("item", {}).get("Container", "")

    def _work() -> "Tuple[Path, str, int]":
        return _download_track(tid, url, container, bus)

    def _ok(result: "Tuple[Path, str, int]") -> None:
        part, ext, nbytes = result
        _finish(tid, success=True, part_path=part, ext=ext, nbytes=nbytes)

    def _err(exc: Exception) -> None:
        print(f"[jellytoast] download failed for {tid}: {exc}", flush=True)
        _finish(tid, success=False)

    from modules.async_io import run_async

    run_async(_work, on_result=_ok, on_error=_err)


def _finish(
    tid: str, *, success: bool, part_path: "Optional[Path]" = None, ext: str = "", nbytes: int = 0
) -> None:
    """Terminal handler for one track — commit or fail, then roll the
    result upward and free the slot. GUI thread."""
    from . import index, store
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()

    _active.discard(tid)
    job = _jobs.pop(tid, None)

    # Cancelled out from under us by remove(): the index rows are
    # already gone, so don't commit (it would re-create an orphan
    # blobs row). Just discard the fragment and move on.
    if tid in _cancelled:
        _cancelled.discard(tid)
        if part_path is not None:
            store.discard_part(part_path)
        _dispatch()
        return

    if success and part_path is not None:
        store.commit_blob(
            tid, part_path, quality="original", codec=(ext if ext != "audio" else ""), bytes_=nbytes
        )
        index.clear_retry(tid)
        index.set_state(tid, "complete")
        bus.download_progress.emit(tid, "complete", 1.0)
    else:
        _record_failure(tid)
        bus.download_progress.emit(tid, "failed", 0.0)

    _propagate(tid)
    if job:
        for parent_id in job["parents"]:
            _bump_parent(parent_id)
    _dispatch()


def _propagate(tid: str) -> None:
    """Walk the edge graph upward from a finished track, recomputing
    each ancestor's state so artist -> album -> track completion rolls
    up. A node only flips to ``complete`` once every child is."""
    from . import index

    seen: "Set[str]" = set()
    frontier = list(index.parents(tid))
    while frontier:
        pid = frontier.pop()
        if pid in seen:
            continue
        seen.add(pid)
        index.recompute_state(pid)
        frontier.extend(index.parents(pid))


def _bump_parent(parent_id: str) -> None:
    """Tick down a cascade root's remaining-track counter and emit its
    aggregate progress; emit the terminal state when it hits zero."""
    from . import index
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()

    pp = _pending.get(parent_id)
    if pp is None:
        return
    pp["remaining"] -= 1
    total = pp["total"]
    done = total - pp["remaining"]
    if pp["remaining"] > 0:
        bus.download_progress.emit(parent_id, "downloading", done / total)
    else:
        del _pending[parent_id]
        state = index.recompute_state(parent_id) or "complete"
        bus.download_progress.emit(parent_id, state, 1.0)


def _collect_subtree(item_id: str) -> "Set[str]":
    """Every item_id at or below ``item_id`` in the edge graph — used by
    ``remove`` to cancel in-flight jobs before the rows are deleted."""
    from . import index

    seen: "Set[str]" = set()
    frontier = [item_id]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(index.children(cur))
    return seen


# ── Download worker (pool thread) ───────────────────────────────────────────


def _download_track(tid: str, url: str, container_hint: str, bus: Any) -> "Tuple[Path, str, int]":
    """Blocking, chunked HTTP GET into a ``.part`` file. Runs on an
    ``async_io`` pool worker. Emits ``download_progress`` as bytes
    arrive (the signal is thread-safe — queued onto the GUI thread).
    Returns ``(part_path, ext, byte_count)``; the GUI-thread ``_finish``
    does the atomic commit so it can honour a mid-flight cancellation.
    Raises on any HTTP / IO error, which ``run_async`` routes to the
    error callback. The ``.part`` is left behind on failure."""
    import requests
    from . import store

    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        ext = _ext_for(resp.headers.get("Content-Type", ""), container_hint)
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0

        part = store.part_path_for(tid, ext)
        got = 0
        last_emit = 0.0
        with open(part, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total:
                    frac = got / total
                    if frac - last_emit >= _PROGRESS_STEP:
                        last_emit = frac
                        bus.download_progress.emit(tid, "downloading", frac)

    return part, ext, got


# ── Queue-level pause / resume / retry ─────────────────────────────────────


def _load_paused_once() -> None:
    """Hydrate ``_paused`` from QSettings on first touch. Deferred (not
    at import) so tests + headless tools that never touch settings can
    import this module without dragging Qt in."""
    global _paused, _paused_loaded
    if _paused_loaded:
        return
    _paused_loaded = True
    try:
        from modules.settings import get_settings

        _paused = bool(get_settings().downloads_paused)
    except Exception:
        _paused = False


def is_paused() -> bool:
    """True when the queue is paused. In-flight jobs may still be
    finishing — pause is "stop popping new work", not "kill the workers"."""
    _load_paused_once()
    return _paused


def pause() -> None:
    """Pause the download queue. In-flight blobs run to completion (a
    partial blob would be discarded bytes), then the queue idles.
    Persists across restart via ``settings.downloads_paused`` and emits
    ``PlayerBus.download_queue_paused`` on transition. Idempotent."""
    global _paused
    _load_paused_once()
    if _paused:
        return
    _paused = True
    _persist_paused(True)
    _emit_paused()


def resume() -> None:
    """Clear the queue-level pause flag and kick the dispatcher so any
    waiting jobs start. Persists across restart and emits
    ``PlayerBus.download_queue_resumed`` on transition. Idempotent — a
    no-op when the queue isn't paused."""
    global _paused
    _load_paused_once()
    if not _paused:
        return
    _paused = False
    _persist_paused(False)
    _emit_resumed()
    _dispatch()


def retry_failed(force: bool = False) -> int:
    """Move every eligible ``failed`` node to ``pending`` and re-enqueue
    the leaf tracks. Returns the count actually re-queued so the UI can
    surface "Retried N downloads". Cascade roots (album / artist /
    playlist) flip back to ``pending`` too — their state will roll up
    again as their child tracks complete.

    Items whose ``retry_after_ts`` is still in the future are skipped so
    a re-fail can't bounce back through the queue instantly. Pass
    ``force=True`` to bypass the backoff window — intended for a future
    user-initiated "Retry now" button."""
    from . import db, index
    from modules.player_state import PlayerBus

    ident = index.server_identity()
    rows = db.query(
        "SELECT * FROM nodes WHERE state = 'failed' AND id LIKE ?",
        (f"{ident}:%",),
    )
    if not rows:
        return 0

    now = int(time.time())
    bus = PlayerBus.get()
    requeued = 0
    for r in rows:
        item_id = r["item_id"]
        kind = r["kind"]
        retry_after = r["retry_after_ts"]
        if not force and retry_after is not None and int(retry_after) > now:
            # Still in backoff — leave failed, skip this round.
            continue
        index.set_state(item_id, "pending")
        bus.download_progress.emit(item_id, "pending", 0.0)
        if kind != "track":
            # Cascade roots don't get a blob job of their own — their
            # leaf tracks do. _propagate / _bump_parent handle the
            # roll-up once those leaves finish.
            continue
        if item_id in _jobs or item_id in _active or item_id in _queue:
            continue
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        if not meta:
            meta = {"Id": item_id}
        _jobs[item_id] = {"item": meta, "parents": set()}
        _queue.append(item_id)
        requeued += 1

    _dispatch()
    return requeued


def get_retry_state(item_id: str) -> "Optional[Dict[str, Any]]":
    """Read-only view of a node's retry bookkeeping for the UI.

    Returns ``{"retry_count", "retry_after_ts", "seconds_until_retry"}``
    for any node that has recorded at least one failure, or ``None`` if
    the node doesn't exist or has never failed. ``seconds_until_retry``
    is the wall-clock countdown clamped at zero — UI can render
    "Retry in 30s" without knowing the backoff schedule."""
    from . import index

    row = index.get_node(item_id)
    if not row:
        return None
    retry_count = int(row.get("retry_count") or 0)
    retry_after_ts = row.get("retry_after_ts")
    if retry_count == 0 and retry_after_ts is None:
        return None
    now = int(time.time())
    seconds_until_retry = max(0, int(retry_after_ts) - now) if retry_after_ts is not None else 0
    return {
        "retry_count": retry_count,
        "retry_after_ts": int(retry_after_ts) if retry_after_ts is not None else None,
        "seconds_until_retry": seconds_until_retry,
    }


def _load_wifi_only_once() -> None:
    """Hydrate ``_wifi_only`` from QSettings on first touch. Same lazy
    pattern as ``_load_paused_once`` — keeps headless tools that never
    touch QSettings from dragging Qt in at import time."""
    global _wifi_only, _wifi_only_loaded
    if _wifi_only_loaded:
        return
    _wifi_only_loaded = True
    try:
        from modules.settings import get_settings

        _wifi_only = bool(get_settings().downloads_wifi_only)
    except Exception:
        _wifi_only = False


def is_wifi_only() -> bool:
    """True when the user has opted into the Wi-Fi-only gate. The gate
    only actually blocks dispatch when ``is_on_metered()`` is also True."""
    _load_wifi_only_once()
    return _wifi_only


def set_wifi_only(value: bool) -> None:
    """Flip the persisted Wi-Fi-only preference. Persists across
    restart, emits ``PlayerBus.downloads_wifi_only_changed`` on
    transition, and kicks ``_dispatch`` when turning off — queued jobs
    that were blocked by the gate get a chance to start. Idempotent."""
    global _wifi_only
    _load_wifi_only_once()
    new_value = bool(value)
    if _wifi_only == new_value:
        return
    _wifi_only = new_value
    _persist_wifi_only(new_value)
    _emit_wifi_only_changed(new_value)
    if not new_value:
        _dispatch()


def mark_metered(value: bool) -> None:
    """Stub seam for the future auto-detect layer. Records whether we
    appear to be on a metered / cellular connection. Transient — never
    persisted — so a stale flag can't survive a restart and lock the
    queue out indefinitely. When the user has opted into Wi-Fi-only and
    this transitions from True to False with queued work waiting,
    kicks ``_dispatch`` so downloads resume."""
    global _on_metered
    new_value = bool(value)
    if _on_metered == new_value:
        return
    was_blocking = _on_metered and _wifi_only
    _on_metered = new_value
    if was_blocking and not new_value:
        _dispatch()


def is_on_metered() -> bool:
    """True when the metered-network stub flag is set. Always False
    until a future auto-detect layer (or a test) calls ``mark_metered``."""
    return _on_metered


def _persist_wifi_only(value: bool) -> None:
    try:
        from modules.settings import get_settings

        get_settings().downloads_wifi_only = bool(value)
    except Exception:
        pass


def _emit_wifi_only_changed(value: bool) -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().downloads_wifi_only_changed.emit(bool(value))
    except Exception:
        pass


def _persist_paused(value: bool) -> None:
    """Write the paused flag into QSettings. Failures here are best-
    effort — the in-memory ``_paused`` is the source of truth for the
    current session, persistence only matters across a restart."""
    try:
        from modules.settings import get_settings

        get_settings().downloads_paused = bool(value)
    except Exception:
        pass


def _emit_paused() -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().download_queue_paused.emit()
    except Exception:
        pass


def _emit_resumed() -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().download_queue_resumed.emit()
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Wipe queue + paused state. Used by tests so order can't leak a
    stuck pause flag or a half-populated queue. Not part of the public
    API."""
    global _paused, _paused_loaded, _wifi_only, _wifi_only_loaded, _on_metered
    _queue.clear()
    _active.clear()
    _jobs.clear()
    _cancelled.clear()
    _pending.clear()
    _paused = False
    _paused_loaded = False
    _wifi_only = False
    _wifi_only_loaded = False
    _on_metered = False
