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
import logging
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

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
_jobs: "Dict[str, Dict[str, Any]]" = {}  # track_id -> {item, parents, total_bytes, got_bytes}
_cancelled: "Set[str]" = set()  # track_ids removed mid-flight
# top_parent_id -> {total, remaining} — drives the aggregate
# download_progress signal for a cascade's user-requested root node.
_pending: "Dict[str, Dict[str, int]]" = {}

# Per-track byte-rate samples for the 1 Hz stats tick: tid -> list of
# (monotonic_time, cumulative_bytes). Trimmed to the last ~3 seconds on
# each chunk write. Only the pool worker for ``tid`` writes its entry;
# only the GUI-thread stats tick reads. Writes use atomic ref swap
# (build the new trimmed list, then ``_rates[tid] = new_list``) so a
# read sees old-or-new, never a torn list. Cleared in ``_finish`` once
# the job lands.
_rates: "Dict[str, List[Tuple[float, int]]]" = {}
# Rolling window for the byte-rate samples. 3 seconds is short enough
# to react to a stall, long enough to smooth chunk-size jitter.
_RATE_WINDOW_S = 3.0

# Session-scoped counters for the aggregate stats signal. Tick up as
# jobs dispatch / fail; both reset to 0 on the drain edge.
_session_total: int = 0
_session_failed: int = 0
# Caller-supplied "expected total tracks" for the current session.
# When a bulk-walk (``library_sync.sync_library``) knows the total
# count upfront — by enumerating album ``ChildCount`` before any
# enqueue — it sets this so the aggregate display reads
# "Downloading 2 of <stable total>" instead of having the right-hand
# number climb as new tracks dispatch. The stats tick emits
# ``max(_session_total, _session_expected_total)`` for the
# ``total_session`` field. Reset on the drain edge alongside
# ``_session_total``.
_session_expected_total: int = 0

# In-flight planning count — incremented when ``download()`` schedules a
# ``_plan`` job onto the thread pool, decremented in both the success
# and error callbacks. ``library_sync`` reads this to backpressure phase
# 2 so it doesn't queue 400 plannings ahead of the first
# ``_download_track`` (FIFO pool starvation, see
# ``planning_in_flight``).
_planning_in_flight: int = 0

# Lazy ETA hard cap (seconds). When the longest-job projection exceeds
# this, we emit -1.0 ("unknown") rather than a number that no longer
# means anything. 12 h matches the §9 very-slow rule.
_ETA_HARD_CAP_S = 12 * 3600

# Stats tick QTimer — owned by the manager, started lazily when the
# first job dispatches (``_dispatch``), stopped after the drain-edge
# emission. Built on demand in the GUI thread; tests can stub it via
# ``_stats_tick`` directly.
_stats_timer: Any = None

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
    ``retry_after_ts`` in one transaction. Also bumps the session-
    failure counter so the drain-edge notification can report
    ``"K downloaded, F failed"``."""
    global _session_failed
    from . import index

    now = int(time.time())
    # Read the prior count up-front so we pick the *next* window based
    # on what this failure makes the count, not the prior value.
    row = index.get_node(item_id)
    prior = int(row.get("retry_count") or 0) if row else 0
    window = backoff_for(prior + 1)
    index.record_failure(item_id, now + window)
    index.set_state(item_id, "failed")
    _session_failed += 1


# ── Public entry points ─────────────────────────────────────────────────────


def enqueue(item: Dict[str, Any]) -> None:
    """Queue ``item`` (a track / album / playlist / artist dict) for
    download. Returns immediately; planning (the provider round-trips
    that expand a cascade) happens on a worker, then the leaf-track
    blob downloads run through the queue. Progress and terminal state
    are reported on the ``download_progress`` bus signal —
    ``(item_id, state, fraction)`` — for both the leaf tracks and the
    user-requested root node."""
    from . import index, snapshot

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
        global _planning_in_flight
        _planning_in_flight = max(0, _planning_in_flight - 1)
        _ingest_plan(item_id, kind, leaves)

    def _plan_err(exc: Exception) -> None:
        global _planning_in_flight
        _planning_in_flight = max(0, _planning_in_flight - 1)
        _record_failure(item_id)
        bus.download_progress.emit(item_id, "failed", 0.0)
        logger.warning("download planning failed for %s: %s", item_id, exc)

    from modules.async_io import run_async

    global _planning_in_flight
    _planning_in_flight += 1
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
        # A descendant shared with a parent OUTSIDE this subtree survives
        # cascade_delete (its refcount stays > 0). Leave its queued /
        # in-flight job ALONE so it keeps downloading and rolls up to the
        # surviving parent — _finish's _propagate re-queries the index
        # (which now points only at surviving parents) and _bump_parent on
        # the removed parent is a no-op. Cancelling it (adding to
        # _cancelled + dropping _jobs) would instead make _finish discard
        # the fragment and skip the cascade, wedging the surviving parent's
        # counter forever.
        if tid != item_id and any(p not in subtree for p in index.parents(tid)):
            continue
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
    from . import db, index, snapshot

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
    from modules.player_state import PlayerBus

    from . import index

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
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _queue.append(lid)

    _dispatch()


def _dispatch() -> None:
    """Start downloads up to the concurrency cap. Called whenever a slot
    might have opened — after ingest and after every terminal. Honours
    the queue-level ``_paused`` flag: a paused queue lets in-flight jobs
    finish but never pops the next. Also honours the Wi-Fi-only gate
    when the network is flagged metered."""
    global _session_total
    _load_paused_once()
    _load_wifi_only_once()
    if _paused:
        if _queue and _session_total == 0:
            logger.info("_dispatch blocked: queue is paused")
        return
    if _wifi_only and _on_metered:
        if _queue and _session_total == 0:
            logger.info("_dispatch blocked: Wi-Fi-only + on metered")
        return
    while len(_active) < _MAX_CONCURRENT and _queue:
        tid = _queue.popleft()
        if tid in _cancelled or tid not in _jobs:
            _cancelled.discard(tid)
            continue
        _active.add(tid)
        _session_total += 1
        _start_download(tid)
    # Stats timer is lazy: first dispatch that puts something into
    # ``_active`` wakes it; later drain stops it again.
    if _active:
        _ensure_stats_timer()


def _start_download(tid: str) -> None:
    """Resolve the stream URL and fire the background GET for one track."""
    from modules.player_state import PlayerBus
    from modules.providers import get_provider
    from modules.settings import get_settings

    from . import index

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
        logger.warning("download failed for %s: %s", tid, exc)
        _finish(tid, success=False)

    from modules.async_io import run_async

    run_async(_work, on_result=_ok, on_error=_err)


def _finish(
    tid: str, *, success: bool, part_path: "Optional[Path]" = None, ext: str = "", nbytes: int = 0
) -> None:
    """Terminal handler for one track — commit or fail, then roll the
    result upward and free the slot. GUI thread."""
    from modules.player_state import PlayerBus

    from . import index, store

    bus = PlayerBus.get()

    _active.discard(tid)
    job = _jobs.pop(tid, None)
    # Per-tid rate samples die with the job — only the in-flight tids
    # contribute to the aggregate speed.
    _rates.pop(tid, None)

    # Cancelled out from under us by remove(): the index rows are
    # already gone, so don't commit (it would re-create an orphan
    # blobs row). Just discard the fragment and move on.
    if tid in _cancelled:
        _cancelled.discard(tid)
        if part_path is not None:
            store.discard_part(part_path)
        _dispatch()
        _maybe_drain_edge()
        return

    # Wrap the commit + roll-up so a failure in store.commit_blob (an
    # ENOSPC on the DB INSERT, a transient os.replace error) can't skip
    # _dispatch()/_maybe_drain_edge() and silently wedge the queue with a
    # phantom in-progress job (the slot was already freed above). A commit
    # failure is downgraded to a track failure so the parent cascade still
    # advances and the track stays retryable.
    try:
        if success and part_path is not None:
            committed = False
            try:
                store.commit_blob(
                    tid,
                    part_path,
                    quality="original",
                    codec=(ext if ext != "audio" else ""),
                    bytes_=nbytes,
                )
                committed = True
            except Exception as e:  # noqa: BLE001
                logger.warning("commit_blob failed for %s: %r — marking failed", tid, e)
            if committed:
                index.clear_retry(tid)
                index.set_state(tid, "complete")
                bus.download_progress.emit(tid, "complete", 1.0)
            else:
                _record_failure(tid)
                bus.download_progress.emit(tid, "failed", 0.0)
        else:
            _record_failure(tid)
            bus.download_progress.emit(tid, "failed", 0.0)

        _propagate(tid)
        if job:
            for parent_id in job["parents"]:
                _bump_parent(parent_id)
    finally:
        _dispatch()
        _maybe_drain_edge()


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
    from modules.player_state import PlayerBus

    from . import index

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
    error callback. Cleans up the ``.part`` on disk-full / write
    failure so repeated retries don't accumulate orphan fragments."""
    import requests

    from . import store

    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        ext = _ext_for(resp.headers.get("Content-Type", ""), container_hint)
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0

        # Cache the per-job totals so the GUI-thread stats tick can
        # compute ETA without re-reading the response headers. ``_jobs``
        # may have been popped from under us by ``remove`` — in which
        # case the dict entry's gone and so is the job, no need to
        # write anywhere.
        job = _jobs.get(tid)
        if job is not None:
            job["total_bytes"] = total

        part = store.part_path_for(tid, ext)
        got = 0
        last_emit = 0.0
        try:
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    # Byte-rate sampling for the aggregate stats tick.
                    # Atomic ref swap: build the new trimmed list, then
                    # point ``_rates[tid]`` at it in one dict-item assign
                    # so the GUI-thread reader sees old-or-new, never a
                    # torn list. ``_jobs[tid]["got_bytes"]`` is a single
                    # write to a dict slot the worker owns; the reader
                    # only reads it under its own GIL slice.
                    _record_byte_sample(tid, got)
                    if job is not None:
                        job["got_bytes"] = got
                    if total:
                        frac = got / total
                        if frac - last_emit >= _PROGRESS_STEP:
                            last_emit = frac
                            bus.download_progress.emit(tid, "downloading", frac)
        except (OSError, IOError):
            # ENOSPC (disk full), permission errors, network read
            # mid-stream — discard the partial fragment so a retry
            # starts clean rather than accumulating orphan .parts on
            # disk. Re-raise so async_io routes to the error callback.
            try:
                store.discard_part(part)
            except Exception:
                pass
            raise

    return part, ext, got


def _record_byte_sample(tid: str, cumulative_bytes: int) -> None:
    """Append a ``(now, cumulative_bytes)`` sample to ``_rates[tid]`` and
    trim to the last ``_RATE_WINDOW_S`` seconds. Atomic ref-swap write:
    the GUI-thread tick reader sees either the old list or the new one,
    never a half-rebuilt list. Called from the pool worker for ``tid``."""
    now = time.monotonic()
    prior = _rates.get(tid) or []
    cutoff = now - _RATE_WINDOW_S
    # Drop samples older than the window, then append the new one. Build
    # a fresh list (not in-place mutation) so the ref-swap on the next
    # line publishes a fully-formed list to any concurrent reader.
    trimmed = [s for s in prior if s[0] >= cutoff]
    trimmed.append((now, cumulative_bytes))
    _rates[tid] = trimmed


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


def resume_pending() -> int:
    """Re-queue every node that was mid-flight when the app was last
    closed. Called once at startup from ``offline.init``.

    Walks the index for nodes in state ``pending`` or ``downloading``,
    resets ``downloading`` rows to ``pending`` (the ``.part`` fragment
    is safe — ``commit_blob`` only writes the ``blobs`` row after the
    atomic rename, so a half-downloaded file is self-evidently
    incomplete and the next attempt overwrites it), and pushes
    track-kind items into ``_queue``. Cascade roots stay in ``pending``
    and roll up again as their child tracks complete.

    Returns the count actually re-queued so callers can log it."""
    from modules.player_state import PlayerBus

    from . import db, index

    ident = index.server_identity()
    rows = db.query(
        "SELECT * FROM nodes WHERE state IN ('pending', 'downloading') "
        "AND id LIKE ?",
        (f"{ident}:%",),
    )
    if not rows:
        return 0

    bus = PlayerBus.get()
    requeued = 0
    for r in rows:
        item_id = r["item_id"]
        kind = r["kind"]
        if r["state"] == "downloading":
            index.set_state(item_id, "pending")
        # The bus signal lets the DownloadsView resurrect a row for
        # rolled-up state on its next reload — without this, freshly-
        # opened pages would show old rows but no live updates.
        bus.download_progress.emit(item_id, "pending", 0.0)
        if kind != "track":
            continue
        if item_id in _jobs or item_id in _active or item_id in _queue:
            continue
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        if not meta:
            meta = {"Id": item_id}
        _jobs[item_id] = {
            "item": meta,
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
        _queue.append(item_id)
        requeued += 1

    if requeued:
        _dispatch()
    return requeued


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
    from modules.player_state import PlayerBus

    from . import db, index

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
        _jobs[item_id] = {
            "item": meta,
            "parents": set(),
            "total_bytes": 0,
            "got_bytes": 0,
        }
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


# ── Aggregate stats tick + drain-edge notification ─────────────────────────


def _compute_rate(samples: "List[Tuple[float, int]]") -> float:
    """Bytes-per-second over the sample window. Needs at least two
    samples to derive a rate — a single sample yields 0.0."""
    if not samples or len(samples) < 2:
        return 0.0
    t0, b0 = samples[0]
    t1, b1 = samples[-1]
    dt = t1 - t0
    if dt <= 0:
        return 0.0
    delta = b1 - b0
    if delta < 0:
        return 0.0
    return delta / dt


def _current_stats() -> "Tuple[int, int, float, float]":
    """One-shot snapshot of the aggregate counters: ``(active,
    total_session, speed_bps, eta_seconds)``. The same payload the 1 Hz
    tick emits. Pure read — safe from any thread, but the contract is
    "GUI-thread only" because that's where the writers live."""
    active = len(_active)
    per_tid_rates: "Dict[str, float]" = {}
    for tid in list(_active):
        per_tid_rates[tid] = _compute_rate(_rates.get(tid) or [])
    speed_bps = sum(per_tid_rates.values())

    # ETA = longest projected remaining for any active job with a known
    # total_bytes > 0 and a positive rate. Hide silly numbers (> 12 h)
    # behind the unknown sentinel.
    eta_candidates: List[float] = []
    for tid, rate in per_tid_rates.items():
        if rate <= 0:
            continue
        job = _jobs.get(tid)
        if not job:
            continue
        total = int(job.get("total_bytes") or 0)
        if total <= 0:
            continue
        got = int(job.get("got_bytes") or 0)
        remaining = max(0, total - got)
        eta_candidates.append(remaining / rate)
    if eta_candidates:
        eta = max(eta_candidates)
        if eta > _ETA_HARD_CAP_S:
            eta = -1.0
    else:
        eta = -1.0
    # When a bulk-walk has registered an expected total upfront,
    # surface ``max`` so the "X of Y" right-hand number stays stable
    # instead of climbing as dispatches roll through.
    total = max(_session_total, _session_expected_total)
    return active, total, float(speed_bps), float(eta)


def reset_session_counters() -> None:
    """Wipe the session-scoped counters used by the aggregate progress
    signal. Called from ``offline.clear_all`` so a destructive sweep
    also clears the "downloading X of Y" state the UI was tracking —
    otherwise the aggregate would keep advertising a queue that no
    longer exists. Emits a final ``(0, 0, 0.0, 0.0)`` so subscribers
    hide their UI immediately."""
    global _session_total, _session_failed, _session_expected_total
    _session_total = 0
    _session_failed = 0
    _session_expected_total = 0
    _stop_stats_timer()
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().download_queue_stats.emit(0, 0, 0.0, 0.0)
    except Exception:
        pass


def set_session_expected_total(n: int) -> None:
    """Caller-supplied "expected tracks" count for the current bulk
    session — clamps the stats signal's ``total_session`` field from
    below so a library-walk display shows a stable total instead of
    one that climbs alongside dispatch."""
    global _session_expected_total
    _session_expected_total = max(0, int(n))
    # Kick a stats tick so the aggregate refreshes immediately rather
    # than waiting up to a second for the next regularly-scheduled
    # one. ``_stats_tick`` is GUI-thread safe via the existing emit.
    try:
        _ensure_stats_timer()
        _stats_tick()
    except Exception:
        pass


def get_queue_stats() -> "Tuple[int, int, float, float]":
    """One-shot read of the aggregate download stats — ``(active,
    total_session, speed_bps, eta_seconds)``. The same payload that the
    ``download_queue_stats`` bus signal carries; intended for callers
    (the future DownloadsView aggregate block) that want to prime their
    UI on construction without waiting for the next tick."""
    return _current_stats()


def get_session_completed() -> int:
    """Tracks finished this session — ``_session_total`` (dispatched)
    minus currently in-flight. Distinct from ``total_session`` in the
    stats signal, which is clamped upward by ``_session_expected_total``
    when a bulk walk pre-counts; that clamp made the simple
    "(total - active) / total" fraction read 100% before any track had
    actually started. UI fraction math should use this against
    ``total_session``."""
    return max(0, _session_total - len(_active))


def get_queue_bytes_progress() -> float:
    """Smooth byte-weighted progress fraction, in ``[0, 1]``. Used by
    the Downloads page aggregate bar to replace the coarser "jobs
    finished / jobs total" ramp with one that climbs continuously as
    bytes arrive.

    The denominator is the same clamped session total carried by
    ``download_queue_stats`` so a bulk-walk pre-count keeps the
    fraction proportional to its expected size, not just whatever's
    been dispatched so far. The numerator is the count of completed
    tracks (each worth 1.0) plus a fractional contribution from every
    currently-active job — ``got_bytes / total_bytes`` once the
    Content-Length lands, ``0`` until then.

    Returns ``0.0`` when the queue is idle. Safe to call from the GUI
    thread: ``_jobs`` value writes from the pool worker are single
    dict-slot updates, structural changes (pop on finish/fail) happen
    on the GUI thread, so the snapshot iteration here can't race."""
    total_session = max(_session_total, _session_expected_total)
    if total_session <= 0:
        return 0.0
    completed = max(0, _session_total - len(_active))
    partial = 0.0
    # Snapshot the active set so a concurrent finish that pops the
    # job out of _jobs can't disappear it mid-iteration.
    for tid in tuple(_active):
        job = _jobs.get(tid)
        if not job:
            continue
        total_b = job.get("total_bytes") or 0
        got_b = job.get("got_bytes") or 0
        if total_b > 0:
            frac = got_b / total_b
            if frac > 1.0:
                frac = 1.0
            partial += frac
        # else: contribution stays 0 until the first chunk reports a
        # Content-Length-backed total.
    return max(0.0, min(1.0, (completed + partial) / total_session))


def _ensure_stats_timer() -> None:
    """Build + start the 1 Hz stats tick on first need. Idempotent — a
    running timer is left alone.

    Safe to call from any thread. ``_dispatch`` runs on whichever
    thread issued ``enqueue`` (often a ``QThreadPool`` worker from
    ``sync_library`` or similar); a ``QTimer`` created on a thread
    without an event loop never fires, so when we're not on the GUI
    thread we hop via ``QTimer.singleShot`` with the QApplication as
    receiver."""
    try:
        from PySide6.QtCore import QCoreApplication, QThread, QTimer
    except Exception:
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    if QThread.currentThread() == app.thread():
        _ensure_stats_timer_gui()
    else:
        QTimer.singleShot(0, app, _ensure_stats_timer_gui)


def _ensure_stats_timer_gui() -> None:
    """GUI-thread half of ``_ensure_stats_timer``. Always called on the
    GUI thread (directly or via ``QTimer.singleShot`` from a worker)."""
    global _stats_timer
    if _stats_timer is not None:
        try:
            if _stats_timer.isActive():
                return
        except Exception:
            pass
    try:
        from PySide6.QtCore import QTimer
    except Exception:
        return
    timer = QTimer()
    timer.setInterval(1000)
    timer.timeout.connect(_stats_tick)
    timer.start()
    _stats_timer = timer


def _stop_stats_timer() -> None:
    """Halt the stats tick if running. Idempotent.

    Safe to call from any thread. The timer was created on the GUI
    thread (see ``_ensure_stats_timer_gui``) and Qt forbids
    starting/stopping a ``QTimer`` from a thread other than its owning
    one — doing so trips ``QObject::killTimer: Timers cannot be stopped
    from another thread`` and can crash the process. Off-thread callers
    (``reset_session_counters`` via ``clear_all`` running on a
    ``QThreadPool`` worker, in particular) hop via
    ``QTimer.singleShot`` like ``_ensure_stats_timer`` does on the
    construction side."""
    try:
        from PySide6.QtCore import QCoreApplication, QThread, QTimer
    except Exception:
        # Headless / no Qt — drop the reference, that's all we can do.
        global _stats_timer
        _stats_timer = None
        return
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() == app.thread():
        _stop_stats_timer_gui()
    else:
        QTimer.singleShot(0, app, _stop_stats_timer_gui)


def _stop_stats_timer_gui() -> None:
    """GUI-thread half of ``_stop_stats_timer``."""
    global _stats_timer
    if _stats_timer is None:
        return
    try:
        _stats_timer.stop()
    except Exception:
        pass
    _stats_timer = None


def _stats_tick() -> None:
    """One 1 Hz emission of ``download_queue_stats``. Reads ``_rates``
    / ``_jobs`` / ``_active`` and emits on the bus. GUI thread."""
    try:
        from modules.player_state import PlayerBus
    except Exception:
        return
    active, total_session, speed_bps, eta = _current_stats()
    PlayerBus.get().download_queue_stats.emit(
        active, total_session, float(speed_bps), float(eta)
    )


def _maybe_drain_edge() -> None:
    """If both ``_active`` and ``_queue`` are empty and at least one job
    has dispatched this drain cycle, fire the completion notification +
    reset session counters. Called after every ``_finish``. Idempotent
    — resets ``_session_total`` to 0 so a re-entry skips the notify
    until the next dispatch."""
    if _active or _queue:
        return
    if _session_total <= 0:
        return
    _emit_drain_complete()


def _emit_drain_complete() -> None:
    """Build + fire the desktop notification for the drain edge, reset
    session counters, stop the stats timer, and emit a final
    ``download_queue_stats(0, 0, 0.0, 0.0)`` so subscribers can hide
    their UI immediately. Notification is gated on
    ``settings.notify_on_download_complete``; counters reset regardless
    so the next drain isn't double-counted."""
    global _session_total, _session_failed, _session_expected_total

    # Gate the notify via settings. Failure to read settings (headless
    # tests, broken QSettings) is treated as "notify allowed" — the
    # bus emission is the contract, the notification is best-effort.
    notify_enabled = True
    try:
        from modules.settings import get_settings

        notify_enabled = bool(get_settings().notify_on_download_complete)
    except Exception:
        notify_enabled = True

    total = _session_total
    failed = _session_failed
    succeeded = max(0, total - failed)

    if notify_enabled and total > 0:
        title, body = _format_drain_message(succeeded, failed)
        try:
            from modules import notifications

            notifications.notify(title, body)
        except Exception:
            # Notifications failure must never crash the queue.
            pass

    _session_total = 0
    _session_failed = 0
    _session_expected_total = 0
    _stop_stats_timer()

    # Final stats emit so subscribers hide their UI immediately.
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().download_queue_stats.emit(0, 0, 0.0, 0.0)
    except Exception:
        pass


def _format_drain_message(succeeded: int, failed: int) -> "Tuple[str, str]":
    """Drain-edge notification copy. Returns ``(title, body)``. Three
    shapes: all-succeeded, mixed, all-failed."""
    if succeeded > 0 and failed == 0:
        title = "Downloads complete"
        body = f"{succeeded} track{'s' if succeeded != 1 else ''} downloaded."
        return title, body
    if succeeded > 0 and failed > 0:
        title = "Downloads complete"
        body = (
            f"{succeeded} track{'s' if succeeded != 1 else ''} downloaded, "
            f"{failed} failed."
        )
        return title, body
    # All-failed (succeeded == 0, failed > 0). Title shifts to flag the
    # failure case so the user sees it at a glance.
    title = "Downloads failed"
    body = f"{failed} download{'s' if failed != 1 else ''} failed."
    return title, body


def _reset_for_tests() -> None:
    """Wipe queue + paused state. Used by tests so order can't leak a
    stuck pause flag or a half-populated queue. Not part of the public
    API."""
    global _paused, _paused_loaded, _wifi_only, _wifi_only_loaded, _on_metered
    global _session_total, _session_failed, _session_expected_total
    global _planning_in_flight
    _queue.clear()
    _active.clear()
    _jobs.clear()
    _cancelled.clear()
    _pending.clear()
    _rates.clear()
    _paused = False
    _paused_loaded = False
    _wifi_only = False
    _wifi_only_loaded = False
    _on_metered = False
    _session_total = 0
    _session_failed = 0
    _session_expected_total = 0
    _planning_in_flight = 0
    _stop_stats_timer()
