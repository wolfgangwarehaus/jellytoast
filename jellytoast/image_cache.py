"""Disk cache for downloaded album / artist / playlist artwork.

Two-tier with the in-memory LRU in ``ui_helpers.py``: memory → disk →
network. The disk tier survives relaunches, so a returning user sees
the album grid paint instantly from disk on cold launch — only items
new to the library hit the network.

Cache identity is the caller-supplied ``cache_key`` string, decoupled
from the URL. That matters for Subsonic / Navidrome: every request
generates a fresh salt → fresh URL, but the same album resolves to
the same cache slot regardless.

Stored format is the *already-scaled, already-rounded* QPixmap as PNG
bytes. That trades disk space for CPU at retrieval time (no rescale,
no rounded-corner painting on read). Typical cover ~50-150KB at
360×360 with rounded corners.
"""

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QImage, QPixmap

# Bound the disk cache. The old 200MB held only ~1500-4000 covers — fine
# for a small library, but a real one (a first external user reported
# ~5,200 albums / 580GB on Navidrome) stores a raw_ source PNG *plus* a
# rounded per-size PNG per album, ~1.2GB total. At 200MB the cache evicted
# faster than it filled and NEVER converged, so every relaunch re-hit the
# network and re-triggered the cold-load stall ("rebooted twice, still
# broken"). 2GB comfortably persists a multi-thousand-album browse across
# launches; on a truly enormous library it still bounds disk use and the
# in-memory tiers keep RAM flat.
_DISK_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024

# Eviction is amortized: every Nth put runs a directory walk + LRU trim.
# A bare put is just a file write, so this keeps the steady-state cost low
# even on large libraries that exceed the cap once. Raised from 50 → 250
# because at the new cap a full cache holds far more files, so the
# whole-dir stat sweep is heavier; running it a fifth as often keeps the
# shared pool free for the cover disk-lookups that gate tile display.
_EVICTION_INTERVAL = 250

_CACHE_DIR: "Optional[Path]" = None
_puts_since_eviction = 0


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
        _CACHE_DIR = base / "covers"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _filename(cache_key: str) -> Path:
    """Hash the cache key for filesystem safety. SHA1 is fine here —
    we need stable identity, not cryptographic strength."""
    h = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{h}.png"


def _raw_filename(sem_key: str) -> Path:
    """Path for the raw-source variant of a semantic key. Stored
    separately from the per-consumer rounded pixmaps so any caller
    asking for any size of the same image can derive locally without
    a network round-trip. The `raw_` prefix on the hashed name makes
    the two namespaces visually distinguishable on disk and ensures
    they never collide at the SHA1 layer."""
    h = hashlib.sha1(("raw|" + sem_key).encode("utf-8")).hexdigest()
    return _cache_dir() / f"raw_{h}.png"


def get(cache_key: str) -> Optional[QPixmap]:
    """Return the cached pixmap for ``cache_key``, or None on miss.
    Touches the file's mtime on hit so LRU eviction sees recent
    reads as warm. Decode failures fall through as a miss; the
    network refetch will overwrite the corrupt entry."""
    path = _filename(cache_key)
    if not path.exists():
        return None
    try:
        pix = QPixmap()
        if not pix.load(str(path), "PNG") or pix.isNull():
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return pix
    except Exception:
        return None


def get_image(cache_key: str) -> Optional[QImage]:
    """``get`` but returning a QImage — safe to call OFF the GUI thread
    (QPixmap is GUI-thread-only). The pooled disk lookup in
    ``ui_helpers.load_image_async`` reads through this; the caller
    converts to QPixmap back on the GUI thread."""
    path = _filename(cache_key)
    if not path.exists():
        return None
    try:
        img = QImage()
        if not img.load(str(path), "PNG") or img.isNull():
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return img
    except Exception:
        return None


# Background-write bookkeeping: writes (PNG encode + file I/O + the AV
# scan each one triggers on Windows) run on the shared pool — done
# synchronously from cover-load callbacks they stalled the GUI thread
# ~8s during a Windows cold boot (2026-06-12 stall tracebacks).
_pending_writes = 0
_writes_lock = threading.Lock()


def flush_pending_writes(timeout_s: float = 5.0) -> bool:
    """Block until queued background writes finish (tests, shutdown,
    sign-out). Returns False on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _writes_lock:
            if _pending_writes == 0:
                return True
        time.sleep(0.01)
    return False


def _write_image_async(path: Path, img: QImage) -> None:
    """Encode + atomically write ``img`` on the shared pool. QImage is
    implicitly shared and nobody mutates after handing it over, so the
    cross-thread move is safe. Unique tmp name — concurrent writes to
    the same slot must not clobber each other's tmp."""
    global _pending_writes
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")

    def _write():
        global _pending_writes
        try:
            try:
                if not img.save(str(tmp), "PNG"):
                    tmp.unlink(missing_ok=True)
                    return
                tmp.replace(path)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            with _writes_lock:
                _pending_writes -= 1

    with _writes_lock:
        _pending_writes += 1
    try:
        from jellytoast.async_io import run_async

        run_async(_write, on_error=lambda _e: None)
    except Exception:
        _write()


def put(cache_key: str, pix: QPixmap) -> None:
    """Persist ``pix`` under ``cache_key``. Best-effort and
    ASYNCHRONOUS: the QPixmap→QImage copy happens here (GUI thread —
    QPixmap isn't thread-safe), the PNG encode + atomic write land on
    the shared pool. A write failure is silent because the in-memory
    tier is still serving this session."""
    global _puts_since_eviction
    if pix.isNull():
        return
    _write_image_async(_filename(cache_key), pix.toImage())
    _puts_since_eviction += 1
    if _puts_since_eviction >= _EVICTION_INTERVAL:
        _puts_since_eviction = 0
        _schedule_eviction()


# Guards against two eviction sweeps running at once: a large-library fill can
# cross the put threshold again before the previous (disk-walking) sweep
# finishes, and two concurrent sweeps double-count `total` and can over-evict
# covers a live read still needs. A plain bool flag is enough — puts (and thus
# scheduling) all originate on the GUI thread, so the check-and-set never races.
_eviction_in_flight = False


def _schedule_eviction() -> None:
    """Run the cap eviction off the GUI thread. ``put`` / ``put_raw``
    fire from cover-load callbacks on the GUI thread; the eviction
    walks ~2000 file stats on a full cache which can hit 50 ms on
    spinning disk. Hand it to async_io so a freshly-arrived cover
    never gates on a stat sweep. Coalesced: only one sweep at a time."""
    global _eviction_in_flight
    if _eviction_in_flight:
        return
    _eviction_in_flight = True

    def _done(_result=None) -> None:
        global _eviction_in_flight
        _eviction_in_flight = False

    try:
        from jellytoast.async_io import run_async

        run_async(_evict_if_over_cap, on_result=_done, on_error=_done)
    except Exception:
        # Fall back to synchronous eviction if async_io import fails
        # (mainly headless test environments).
        try:
            _evict_if_over_cap()
        finally:
            _eviction_in_flight = False


def _evict_if_over_cap() -> None:
    """Walk the cache dir, sum sizes, delete oldest by mtime until
    under cap. Cheap (a few hundred file stats on a typical cache)
    and amortized over many puts so it doesn't hit the hot path."""
    try:
        entries = []
        total = 0
        for entry in _cache_dir().iterdir():
            if not entry.is_file() or entry.suffix != ".png":
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, entry))
            total += st.st_size
        if total <= _DISK_CACHE_MAX_BYTES:
            return
        entries.sort(key=lambda e: e[0])
        for _, size, path in entries:
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
            if total <= _DISK_CACHE_MAX_BYTES:
                break
    except OSError:
        pass


def get_raw(sem_key: str) -> Optional[QImage]:
    """Return the cached raw-source QImage for ``sem_key``, or None on
    miss. Pairs with ``put_raw`` to feed the in-memory L2 raw-image
    cache: when the album-grid tile loaded an image last session, its
    raw source survives on disk under this key so the next session's
    bar / mini / np-page can derive locally instead of refetching."""
    path = _raw_filename(sem_key)
    if not path.exists():
        return None
    try:
        img = QImage()
        if not img.load(str(path), "PNG") or img.isNull():
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return img
    except Exception:
        return None


def put_raw(sem_key: str, img: QImage) -> None:
    """Persist the raw decoded source image under its semantic key.
    Best-effort and asynchronous — same pooled write as ``put``."""
    global _puts_since_eviction
    if img.isNull():
        return
    _write_image_async(_raw_filename(sem_key), img)
    _puts_since_eviction += 1
    if _puts_since_eviction >= _EVICTION_INTERVAL:
        _puts_since_eviction = 0
        _schedule_eviction()


def clear() -> None:
    """Wipe the entire cover cache. Called on sign-out so a different
    user / server doesn't inherit the previous session's covers.

    Drain in-flight pooled writes FIRST: ``put`` / ``put_raw`` now
    rename onto disk from a worker thread, so a write enqueued just
    before sign-out could otherwise land *after* this unlink sweep and
    resurrect a stale cover under a colliding id — defeating the very
    isolation this wipe exists to enforce. The GUI thread runs ``clear``
    synchronously, so no fresh write can be enqueued mid-sweep once the
    drain returns."""
    flush_pending_writes()
    try:
        for entry in _cache_dir().iterdir():
            try:
                entry.unlink()
            except OSError:
                continue
    except OSError:
        pass
