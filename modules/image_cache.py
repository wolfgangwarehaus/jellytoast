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
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QImage, QPixmap


# Bound the disk cache. 200MB holds ~1500-4000 covers at typical
# sizes — comfortably above any realistic music library.
_DISK_CACHE_MAX_BYTES = 200 * 1024 * 1024

# Eviction is amortized: every Nth put runs a directory walk + LRU
# trim. A bare put is just a file write, so this keeps the steady-
# state cost low even on large libraries that exceed the cap once.
_EVICTION_INTERVAL = 50

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


def put(cache_key: str, pix: QPixmap) -> None:
    """Persist ``pix`` under ``cache_key``. Best-effort: a write
    failure is silent because the in-memory tier is still serving
    this session. Atomic via tempfile + rename so a partial write
    can't yield a corrupt PNG."""
    global _puts_since_eviction
    if pix.isNull():
        return
    path = _filename(cache_key)
    tmp = path.with_suffix(".png.tmp")
    try:
        if not pix.save(str(tmp), "PNG"):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _puts_since_eviction += 1
    if _puts_since_eviction >= _EVICTION_INTERVAL:
        _puts_since_eviction = 0
        _schedule_eviction()


def _schedule_eviction() -> None:
    """Run the cap eviction off the GUI thread. ``put`` / ``put_raw``
    fire from cover-load callbacks on the GUI thread; the eviction
    walks ~2000 file stats on a full cache which can hit 50 ms on
    spinning disk. Hand it to async_io so a freshly-arrived cover
    never gates on a stat sweep."""
    try:
        from modules.async_io import run_async

        run_async(_evict_if_over_cap, on_error=lambda _e: None)
    except Exception:
        # Fall back to synchronous eviction if async_io import fails
        # (mainly headless test environments).
        _evict_if_over_cap()


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
    Best-effort. Same atomic-rename pattern as ``put`` so a partial
    write can't corrupt the slot."""
    global _puts_since_eviction
    if img.isNull():
        return
    path = _raw_filename(sem_key)
    tmp = path.with_suffix(".png.tmp")
    try:
        if not img.save(str(tmp), "PNG"):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _puts_since_eviction += 1
    if _puts_since_eviction >= _EVICTION_INTERVAL:
        _puts_since_eviction = 0
        _schedule_eviction()


def clear() -> None:
    """Wipe the entire cover cache. Called on sign-out so a different
    user / server doesn't inherit the previous session's covers."""
    try:
        for entry in _cache_dir().iterdir():
            try:
                entry.unlink()
            except OSError:
                continue
    except OSError:
        pass
