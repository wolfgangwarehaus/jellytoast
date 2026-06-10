"""Path resolution for downloads — the only per-OS file in the package.

Downloads are **user data, not a cache** (design doc §6): they must not
live in ``QStandardPaths.CacheLocation``, which the OS, "clean my disk"
tools, and iOS-under-storage-pressure can purge. They go in
``AppDataLocation`` — or a user-configurable location (an external
drive, an SD card) once that setting lands in Phase 6.

``QStandardPaths`` resolves the per-OS base with no branching:

    Linux    ~/.local/share/jellytoast/downloads/
    Windows  %LOCALAPPDATA%\\jellytoast\\downloads\\
    macOS    ~/Library/Application Support/jellytoast/downloads/

The only genuine forks — and why this is the package's lone per-OS file
— are the configurable-location override and the future iOS no-backup
flag. ``blobs`` stores **relative** paths (``<sha2>/<sha>.<ext>``);
everything resolves against :func:`downloads_dir` at runtime, so moving
or backing up the downloads folder Just Works (the Finamp iOS lesson).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths

_DOWNLOADS_DIR: "Optional[Path]" = None


def _app_data_base() -> Path:
    """Per-OS ``AppDataLocation`` root. Same root ``disk_cache.py``'s
    ``view_cache`` and ``downloads.db`` live under."""
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))


def _configured_override() -> "Optional[Path]":
    """User-chosen downloads location, if set. Lazy-imported and
    defensively guarded — same pattern as ``disk_cache._server_scope``
    — so this module stays importable from test contexts without a
    fully-initialised settings store. Returns ``None`` until the
    ``download_location`` setting lands in Phase 6."""
    try:
        from jellytoast.settings import get_settings

        raw = getattr(get_settings(), "download_location", "") or ""
        return Path(raw) if raw else None
    except Exception:
        return None


def downloads_dir() -> Path:
    """Absolute base directory for downloaded audio blobs. Created on
    first call (and cached for the process). Honours the configurable
    override when set, else ``AppDataLocation/downloads/``."""
    global _DOWNLOADS_DIR
    if _DOWNLOADS_DIR is None:
        base = _configured_override() or (_app_data_base() / "downloads")
        base.mkdir(parents=True, exist_ok=True)
        _DOWNLOADS_DIR = base
    return _DOWNLOADS_DIR


def db_path() -> Path:
    """Absolute path to ``downloads.db``. Lives in ``AppDataLocation``
    directly (not under ``downloads/``) so it survives a user pointing
    the downloads folder at a removable drive — the index is the
    authoritative record and stays on internal storage."""
    base = _app_data_base()
    base.mkdir(parents=True, exist_ok=True)
    return base / "downloads.db"


def resolve(rel_path: str) -> Path:
    """Absolute path for a ``blobs.rel_path`` value."""
    return downloads_dir() / rel_path


def to_relative(abs_path: Path) -> str:
    """Inverse of :func:`resolve` — the string to persist in
    ``blobs.rel_path``. Raises ``ValueError`` if ``abs_path`` is not
    under the downloads dir, which would be a store bug."""
    return str(Path(abs_path).relative_to(downloads_dir()))
