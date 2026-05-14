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
_queue: "Deque[str]" = collections.deque()      # track item_ids waiting
_active: "Set[str]" = set()                     # track item_ids in flight
_jobs: "Dict[str, Dict[str, Any]]" = {}         # track_id -> {item, parents}
_cancelled: "Set[str]" = set()                  # track_ids removed mid-flight
# top_parent_id -> {total, remaining} — drives the aggregate
# download_progress signal for a cascade's user-requested root node.
_pending: "Dict[str, Dict[str, int]]" = {}


def _ext_for(content_type: str, container_hint: str) -> str:
    """Best-guess file extension from the response Content-Type, falling
    back to the item's ``Container`` metadata, then a neutral default."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CTYPE_EXT:
        return _CTYPE_EXT[ct]
    hint = (container_hint or "").strip().lower().lstrip(".")
    return hint or "audio"


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
        index.set_state(item_id, "failed")
        bus.download_progress.emit(item_id, "failed", 0.0)
        print(f"[JellyToast] download planning failed for {item_id}: {exc}",
              flush=True)

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
            _cancelled.add(tid)        # let the in-flight GET unwind
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

    Each visited node is upserted; each parent->child relationship is
    linked. ``requested`` is True only for the root the user actually
    asked for — children are pulled in as ``requested = 0``."""
    from . import snapshot, index

    item_id = item.get("Id", "")
    kind = snapshot.kind_of(item)
    parent_meta, children = snapshot.freeze(item)
    index.upsert_node(item_id, kind, parent_meta, requested=requested)

    if kind == "track" or not children:
        return [item] if kind == "track" else []

    leaves: List[Dict[str, Any]] = []
    for child in children:
        child_id = child.get("Id", "")
        if not child_id:
            continue
        leaves.extend(_plan(child, requested=False))
        index.link(item_id, child_id)
    return leaves


# ── Ingest + dispatch (GUI thread) ──────────────────────────────────────────

def _ingest_plan(top_id: str, top_kind: str,
                 leaves: List[Dict[str, Any]]) -> None:
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
    might have opened — after ingest and after every terminal."""
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
    bus = PlayerBus.get()

    # Subsonic rotates salt/token per request — resolve at fetch time.
    url = get_provider().get_audio_stream_url(tid)
    if not url:
        index.set_state(tid, "failed")
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
        print(f"[JellyToast] download failed for {tid}: {exc}", flush=True)
        _finish(tid, success=False)

    from modules.async_io import run_async
    run_async(_work, on_result=_ok, on_error=_err)


def _finish(tid: str, *, success: bool,
            part_path: "Optional[Path]" = None,
            ext: str = "", nbytes: int = 0) -> None:
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
        store.commit_blob(tid, part_path, quality="original",
                          codec=(ext if ext != "audio" else ""),
                          bytes_=nbytes)
        index.set_state(tid, "complete")
        bus.download_progress.emit(tid, "complete", 1.0)
    else:
        index.set_state(tid, "failed")
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

def _download_track(tid: str, url: str, container_hint: str,
                     bus: Any) -> "Tuple[Path, str, int]":
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


# ── Phase 6 skeletons ───────────────────────────────────────────────────────

def pause(item_id: str) -> None:
    """Pause an in-flight download; its ``.part`` file is kept for
    resume. Phase 6."""
    raise NotImplementedError("offline.manager.pause — Phase 6")


def resume(item_id: str) -> None:
    """Resume a paused or failed download from its ``.part`` file (HTTP
    range request where the server supports it). Phase 6."""
    raise NotImplementedError("offline.manager.resume — Phase 6")


def retry_failed() -> None:
    """Re-enqueue every node in state ``failed``. Phase 6."""
    raise NotImplementedError("offline.manager.retry_failed — Phase 6")
